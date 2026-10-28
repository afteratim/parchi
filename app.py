import os, re, io, csv, base64
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file
from PIL import Image, ImageOps, ImageEnhance
import pytesseract
from pytesseract import Output

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024

DATE_PATTERNS = [
    re.compile(r'(?<!\d)(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(20\d{2})(?!\d)'),
    re.compile(r'(?<!\d)(20\d{2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{1,2})(?!\d)')
]


def prep_image(image):
    image = ImageOps.exif_transpose(image).convert('RGB')
    # Upscale small phone photos and improve contrast without destroying pen strokes.
    if image.width < 1400:
        scale = min(2.0, 1400 / image.width)
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    gray = ImageOps.grayscale(image)
    gray = ImageEnhance.Contrast(gray).enhance(1.5)
    return gray


def normalize_date(text):
    for idx, pat in enumerate(DATE_PATTERNS):
        m = pat.search(text)
        if not m:
            continue
        try:
            if idx == 0:
                d, mo, y = map(int, m.groups())
            else:
                y, mo, d = map(int, m.groups())
            return datetime(y, mo, d).strftime('%d-%m-%Y')
        except ValueError:
            pass
    return ''


def ocr_page(image):
    gray = prep_image(image)
    # PSM 6 works reasonably for ruled forms; word boxes let us reconstruct text lines.
    data = pytesseract.image_to_data(gray, config='--oem 3 --psm 6', output_type=Output.DICT)
    words = []
    for i, raw in enumerate(data['text']):
        word = (raw or '').strip()
        if not word:
            continue
        try:
            conf = float(data['conf'][i])
        except (ValueError, TypeError):
            conf = -1
        words.append({
            'text': word, 'conf': conf,
            'x': int(data['left'][i]), 'y': int(data['top'][i]),
            'w': int(data['width'][i]), 'h': int(data['height'][i]),
            'block': data['block_num'][i], 'par': data['par_num'][i], 'line': data['line_num'][i]
        })
    # Group words by physical OCR line.
    grouped = {}
    for w in words:
        key = (w['block'], w['par'], w['line'])
        grouped.setdefault(key, []).append(w)
    lines = []
    for group in grouped.values():
        group.sort(key=lambda x: x['x'])
        text = ' '.join(x['text'] for x in group)
        lines.append({'text': text, 'words': group, 'y': min(x['y'] for x in group)})
    lines.sort(key=lambda x: x['y'])
    all_text = '\n'.join(line['text'] for line in lines)
    date = normalize_date(all_text)

    # Detect table rows using a row-like GR token or a CTNS + GR combination.
    records = []
    current_heading = ''
    for line in lines:
        text = line['text'].strip()
        compact = re.sub(r'\s+', '', text.upper())
        # Ignore date and column headers.
        if re.search(r'\b(CTNS|CONG|P\.?G\.?|G\.R\.?|DATE)\b', text, re.I):
            continue
        # GR fields commonly look like 1107594/19, 5376/10, or split by OCR spaces.
        candidates = re.findall(r'(?<!\d)(\d{4,9})\s*[/|\\-]\s*(\d{1,3})(?!\d)', text)
        if candidates:
            gr, slash_qty = candidates[0]
            # The CTNS value is typically the first short standalone number to the left of the GR column.
            nums = []
            for w in line['words']:
                t = re.sub(r'[^0-9]', '', w['text'])
                if t and len(t) <= 3:
                    nums.append((w['x'], t, w['conf']))
            # Prefer a small number before the GR token; otherwise fall back to slash quantity.
            gr_word_x = min((w['x'] for w in line['words'] if re.sub(r'\D', '', w['text']).startswith(gr[:4])), default=10**9)
            left_nums = [n for n in nums if n[0] < gr_word_x and 0 < int(n[1]) <= 500]
            ctns = left_nums[0][1] if left_nums else ''
            gr_word = next((w for w in line['words'] if gr in re.sub(r'\D', '', w['text'])), None)
            avg_conf = sum(max(0, w['conf']) for w in line['words']) / max(1, len(line['words']))
            uncertain = not ctns or avg_conf < 45 or len(gr) < 5
            records.append({
                'ctns': ctns,
                'gr': gr,
                'date': date,
                'heading': current_heading,
                'raw': text,
                'confidence': round(avg_conf),
                'uncertain': uncertain,
                'source_line': text,
                'checked': False
            })
            continue
        # A heading is generally a text-only line with no long digit run and no table labels.
        letters = re.sub(r'[^A-Za-z]', '', text)
        digits = re.sub(r'\D', '', text)
        if len(letters) >= 3 and len(digits) < 4 and not re.search(r'\b(CTNS|CONG|DATE|G\.R)\b', text, re.I):
            heading = re.sub(r'[^A-Za-z0-9 .&-]', '', text).strip(' .-')
            if heading and heading.upper() not in {'D F C', 'DFC'}:
                current_heading = heading

    # Mark likely duplicates without removing them.
    seen = set()
    for rec in records:
        key = (rec['date'], rec['gr'])
        rec['duplicate'] = key in seen
        seen.add(key)
    return {'date': date, 'records': records, 'raw_text': all_text}


@app.get('/')
def index():
    return render_template('index.html')


@app.post('/api/extract')
def extract():
    uploads = request.files.getlist('images')
    if not uploads or not any(f.filename for f in uploads):
        return jsonify({'error': 'Please select at least one image.'}), 400
    pages = []
    for file in uploads:
        if not file.filename:
            continue
        try:
            image = Image.open(file.stream)
            result = ocr_page(image)
            pages.append({'filename': file.filename, **result})
        except Exception as exc:
            pages.append({'filename': file.filename, 'date': '', 'records': [], 'raw_text': '', 'error': str(exc)})
    # Duplicate warnings across all uploaded pages.
    seen = set()
    for page in pages:
        for rec in page.get('records', []):
            key = (rec.get('date') or page.get('date') or '', rec.get('gr', '').strip())
            rec['duplicate'] = key in seen
            if key[1]:
                seen.add(key)
    return jsonify({'pages': pages})


@app.post('/api/export')
def export_csv():
    payload = request.get_json(silent=True) or {}
    records = payload.get('records', [])
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n')
    for r in records:
        gr = str(r.get('gr', '')).strip()
        date = str(r.get('date', '')).strip()
        ctns = str(r.get('ctns', '')).strip()
        output_line = f'{date} - {ctns} CRTNS' if date and ctns else ''
        writer.writerow([gr, output_line])
    data = io.BytesIO(output.getvalue().encode('utf-8-sig'))
    data.seek(0)
    return send_file(data, mimetype='text/csv; charset=utf-8', as_attachment=True, download_name='parchi_extraction.csv')


@app.get('/health')
def health():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '10000')))
