import os
import re
import io
import csv
from datetime import datetime
from statistics import mean

from flask import Flask, render_template, request, jsonify, send_file
from PIL import Image, ImageOps, ImageEnhance, ImageFilter
import pytesseract
from pytesseract import Output

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

DATE_PATTERNS = [
    re.compile(r"(?<!\d)(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(20\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(20\d{2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{1,2})(?!\d)"),
]


def normalize_date(text):
    for idx, pattern in enumerate(DATE_PATTERNS):
        match = pattern.search(text)
        if not match:
            continue
        try:
            if idx == 0:
                day, month, year = map(int, match.groups())
            else:
                year, month, day = map(int, match.groups())
            return datetime(year, month, day).strftime("%d-%m-%Y")
        except ValueError:
            continue
    return ""


def prep_image(image):
    image = ImageOps.exif_transpose(image).convert("RGB")
    # Upscale and improve contrast; keep the original colour/shape information out of OCR.
    if image.width < 1600:
        scale = min(2.2, 1600 / image.width)
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    gray = ImageOps.grayscale(image)
    gray = ImageEnhance.Contrast(gray).enhance(1.65)
    gray = ImageEnhance.Sharpness(gray).enhance(1.25)
    return gray


def _ocr_words(gray, psm):
    data = pytesseract.image_to_data(
        gray,
        config=f"--oem 3 --psm {psm} -c preserve_interword_spaces=1",
        output_type=Output.DICT,
    )
    words = []
    for i, raw in enumerate(data["text"]):
        text = (raw or "").strip()
        if not text:
            continue
        try:
            confidence = float(data["conf"][i])
        except (ValueError, TypeError):
            confidence = -1.0
        x, y = int(data["left"][i]), int(data["top"][i])
        w, h = int(data["width"][i]), int(data["height"][i])
        words.append({
            "text": text, "conf": confidence, "x": x, "y": y, "w": w, "h": h,
            "cx": x + w / 2, "cy": y + h / 2,
            "block": data["block_num"][i], "par": data["par_num"][i],
            "line": data["line_num"][i],
        })
    return words


def _lines_from_words(words):
    grouped = {}
    for word in words:
        key = (word["block"], word["par"], word["line"])
        grouped.setdefault(key, []).append(word)
    lines = []
    for group in grouped.values():
        group.sort(key=lambda word: word["x"])
        lines.append({
            "text": " ".join(word["text"] for word in group),
            "words": group,
            "y": mean(word["cy"] for word in group),
        })
    lines.sort(key=lambda line: line["y"])
    return lines


def _row_clusters(words, image_width, image_height):
    """Cluster OCR words by vertical position so a GR and CTNS needn't share an OCR line."""
    if not words:
        return []
    # A table row is much taller than the handwriting strokes. Use a modest tolerance.
    tolerance = max(13, min(34, image_height * 0.018))
    ordered = sorted(words, key=lambda word: word["cy"])
    clusters = []
    for word in ordered:
        best = None
        best_distance = None
        for cluster in reversed(clusters[-4:]):
            center = mean(item["cy"] for item in cluster)
            distance = abs(word["cy"] - center)
            if distance <= tolerance and (best_distance is None or distance < best_distance):
                best, best_distance = cluster, distance
        if best is None:
            clusters.append([word])
        else:
            best.append(word)
    for cluster in clusters:
        cluster.sort(key=lambda word: word["x"])
    return clusters


def _digits(text):
    # Common OCR substitutions in handwritten numbers. These are only used to propose
    # candidates; uncertain candidates are highlighted for human correction.
    text = text.upper().replace("O", "0").replace("Q", "0").replace("I", "1").replace("L", "1")
    return re.sub(r"\D", "", text)


def _numeric_candidates(cluster, image_width):
    """Return plausible GR candidates, including numbers split over adjacent OCR tokens."""
    # GR numbers normally occupy the middle column, not the far-left CTNS column.
    middle = [w for w in cluster if 0.10 * image_width <= w["cx"] <= 0.68 * image_width]
    candidates = []

    for word in middle:
        digits = _digits(word["text"])
        if 4 <= len(digits) <= 9:
            candidates.append((word["cx"], digits, word["conf"], [word]))

    # OCR may split a handwritten GR into adjacent pieces, e.g. '150' + '096'.
    # Join only nearby digit-like tokens and mark the result uncertain.
    for start in range(len(middle)):
        joined = ""
        used = []
        previous = None
        for word in middle[start:start + 4]:
            piece = _digits(word["text"])
            if not piece or len(piece) > 6:
                break
            if previous is not None:
                gap = word["x"] - (previous["x"] + previous["w"])
                if gap > image_width * 0.035:
                    break
            joined += piece
            used.append(word)
            previous = word
            if 4 <= len(joined) <= 9 and len(used) > 1:
                candidates.append((mean(w["cx"] for w in used), joined,
                                   mean(w["conf"] for w in used), used))
            if len(joined) >= 9:
                break

    # Prefer candidates with more digits, then higher OCR confidence.
    unique = {}
    for x, value, conf, used in candidates:
        key = (round(x / max(1, image_width) * 100), value)
        score = (len(value), conf)
        if key not in unique or score > unique[key][0]:
            unique[key] = (score, x, value, conf, used)
    return sorted(
        [(v[1], v[2], v[3], v[4]) for v in unique.values()],
        key=lambda item: (len(item[1]), item[2]),
        reverse=True,
    )


def _ctns_for_cluster(cluster, gr_x, image_width):
    # CTNS is the left-most table column. Prefer the closest small integer to the GR row.
    left_words = [w for w in cluster if w["cx"] < min(gr_x, image_width * 0.22)]
    options = []
    for word in left_words:
        digits = _digits(word["text"])
        if 1 <= len(digits) <= 3:
            value = int(digits)
            if 0 < value <= 500:
                options.append((abs(word["cx"] - image_width * 0.07), word["conf"], str(value)))
    if not options:
        return "", -1
    options.sort(key=lambda item: (item[0], -item[1]))
    return options[0][2], options[0][1]


def _candidate_rows(words, image_width, image_height, date, heading_seed=""):
    clusters = _row_clusters(words, image_width, image_height)
    records = []
    current_heading = heading_seed

    for cluster in clusters:
        cluster_text = " ".join(w["text"] for w in cluster).strip()
        if not cluster_text:
            continue
        if re.search(r"\b(CTNS|CONG|P\.?G\.?|DATE)\b", cluster_text, re.I):
            continue

        gr_candidates = _numeric_candidates(cluster, image_width)
        if gr_candidates:
            # Longest plausible number is preferred; if multiple candidates are close,
            # use the one nearest the usual GR-column center.
            gr_candidates.sort(
                key=lambda item: (len(item[1]), item[2], -abs(item[0] - image_width * 0.34)),
                reverse=True,
            )
            gr_x, gr, gr_conf, gr_words = gr_candidates[0]
            ctns, ctns_conf = _ctns_for_cluster(cluster, gr_x, image_width)

            # Exclude the slash suffix (e.g. /25) if OCR joined it to a GR token.
            # Parchi GR values are typically 4–9 digits; preserve the detected digits.
            avg_conf = mean(max(0, w["conf"]) for w in cluster)
            split_gr = len(gr_words) > 1
            uncertain = (
                not ctns or avg_conf < 45 or gr_conf < 35 or len(gr) < 5 or split_gr
            )
            records.append({
                "ctns": ctns,
                "gr": gr,
                "date": date,
                "heading": current_heading,
                "raw": cluster_text,
                "confidence": round(avg_conf),
                "uncertain": bool(uncertain),
                "source_line": cluster_text,
                "checked": False,
            })
            continue

        # A text-only row can be a section heading (e.g. BOMBAY JALANDHAR / SURAT).
        letters = re.sub(r"[^A-Za-z]", "", cluster_text)
        digits = re.sub(r"\D", "", cluster_text)
        if len(letters) >= 4 and len(digits) < 4:
            heading = re.sub(r"[^A-Za-z0-9 .&'-]", "", cluster_text).strip(" .-")
            if heading and heading.upper() not in {"D F C", "DFC", "CTNS", "CONG"}:
                current_heading = heading

    return records


def ocr_page(image):
    gray = prep_image(image)
    # Run multiple Tesseract page segmentation modes. Handwritten forms often fail in PSM 6
    # alone; choose the pass that finds the most plausible GR rows.
    pass_results = []
    all_raw_text = []
    for psm in (6, 11, 4):
        try:
            words = _ocr_words(gray, psm)
        except Exception:
            continue
        lines = _lines_from_words(words)
        raw_text = "\n".join(line["text"] for line in lines)
        date = normalize_date(raw_text)
        records = _candidate_rows(words, gray.width, gray.height, date)
        pass_results.append((len(records), sum(not r["uncertain"] for r in records), records, date, raw_text))
        if raw_text:
            all_raw_text.append(f"[PSM {psm}]\n{raw_text}")

    if not pass_results:
        raise RuntimeError("Tesseract did not return OCR data. Check that Tesseract is installed.")

    # Prefer more extracted rows, then more rows with confident OCR.
    pass_results.sort(key=lambda item: (item[0], item[1]), reverse=True)
    _, _, records, date, _ = pass_results[0]

    # If OCR could not read a date, use another pass's date when available.
    if not date:
        date = next((result[3] for result in pass_results if result[3]), "")
        for record in records:
            if not record["date"]:
                record["date"] = date

    seen = set()
    for record in records:
        key = (record.get("date", ""), record.get("gr", ""))
        record["duplicate"] = key in seen
        seen.add(key)

    return {"date": date, "records": records, "raw_text": "\n\n".join(all_raw_text)}


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/extract")
def extract():
    uploads = request.files.getlist("images")
    if not uploads or not any(file.filename for file in uploads):
        return jsonify({"error": "Please select at least one image."}), 400

    pages = []
    for file in uploads:
        if not file.filename:
            continue
        try:
            image = Image.open(file.stream)
            result = ocr_page(image)
            pages.append({"filename": file.filename, **result})
        except Exception as exc:
            pages.append({
                "filename": file.filename, "date": "", "records": [],
                "raw_text": "", "error": str(exc)
            })

    # Warn about duplicates across pages; never remove a row automatically.
    seen = set()
    for page in pages:
        for record in page.get("records", []):
            gr = str(record.get("gr", "")).strip()
            date = str(record.get("date") or page.get("date") or "").strip()
            key = (date, gr)
            record["duplicate"] = bool(gr) and key in seen
            if gr:
                seen.add(key)
    return jsonify({"pages": pages})


@app.post("/api/export")
def export_csv():
    payload = request.get_json(silent=True) or {}
    records = payload.get("records", [])
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for record in records:
        gr = str(record.get("gr", "")).strip()
        date = str(record.get("date", "")).strip()
        ctns = str(record.get("ctns", "")).strip()
        output_line = f"{date} - {ctns} CRTNS" if date and ctns else ""
        writer.writerow([gr, output_line])
    data = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    data.seek(0)
    return send_file(
        data, mimetype="text/csv; charset=utf-8",
        as_attachment=True, download_name="parchi_extraction.csv"
    )


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
