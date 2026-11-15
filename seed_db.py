import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from decimal import Decimal

from sqlalchemy import select

from app.main import MenuItem, SessionLocal, init_db


ROOT = Path(__file__).resolve().parent
IMAGE_ROOTS = (ROOT / 'pictures', ROOT / 'static' / 'images' / 'menu')
DEFAULT_IMAGE_PATH = '/logo.png'
IMAGE_EXTENSIONS = {'.avif', '.gif', '.jpeg', '.jpg', '.png', '.svg', '.webp'}
CYRILLIC_TO_LATIN = str.maketrans({
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
})
DEMO_ITEMS = {
    ('классический кальян', 'кальяны'): ('Чаша на выбор', Decimal('1200')),
    ('фруктовая тарелка', 'закуски'): ('Сезонные фрукты', Decimal('650')),
    ('лимонад домашний', 'напитки'): ('Цитрус и мята', Decimal('350')),
    ('чайник чая', 'напитки'): ('Чай на выбор', Decimal('500')),
}


def normalize_name(value):
    text = unicodedata.normalize('NFKD', str(value)).casefold().translate(CYRILLIC_TO_LATIN)
    return re.sub(r'[^a-z0-9]+', ' ', text).strip()


def preferred_image_path(image):
    webp = image.with_suffix('.webp')
    return webp if webp.is_file() else image


def local_image_path(record, image_files=None):
    image_name = record.get('source_image')
    if image_name and Path(image_name).name == image_name:
        original = ROOT / 'pictures' / 'stories_kamiqr' / 'menuItems' / image_name
        if original.is_file():
            return '/' + preferred_image_path(original).relative_to(ROOT).as_posix()

    files = image_files if image_files is not None else [
        image for root in IMAGE_ROOTS if root.exists()
        for image in root.rglob('*')
        if image.is_file() and image.suffix.lower() in IMAGE_EXTENSIONS
        and (image.suffix.lower() == '.webp' or not image.with_suffix('.webp').is_file())
    ]
    title = normalize_name(record.get('title', ''))
    if title:
        title_tokens = set(title.split())
        best_path = None
        best_score = 0.0
        for image in files:
            stem = image.stem.removesuffix('_thumb')
            if re.fullmatch(r'[0-9a-f]{24,}', stem, re.I):
                continue
            candidate = normalize_name(stem)
            if not candidate:
                continue
            candidate_tokens = set(candidate.split())
            overlap = len(title_tokens & candidate_tokens) / max(len(title_tokens), len(candidate_tokens))
            score = max(SequenceMatcher(None, title, candidate).ratio(), overlap)
            if score > best_score:
                best_path, best_score = image, score
        if best_path is not None and best_score >= 0.42:
            return '/' + preferred_image_path(best_path).resolve().relative_to(ROOT).as_posix()

    return DEFAULT_IMAGE_PATH


def seed_menu(records):
    init_db()
    imported_ids = {record['source_id'] for record in records if record.get('source_id')}
    image_files = [
        image for root in IMAGE_ROOTS if root.exists()
        for image in root.rglob('*')
        if image.is_file() and image.suffix.lower() in IMAGE_EXTENSIONS
        and (image.suffix.lower() == '.webp' or not image.with_suffix('.webp').is_file())
    ]
    stats = {'inserted': 0, 'updated': 0, 'without_image': 0}

    total = len(imported_ids)
    with SessionLocal() as db:
        current_items = db.scalars(select(MenuItem)).all()
        by_source_id = {item.source_id: item for item in current_items if item.source_id}
        unlinked_by_key = {}
        for item in current_items:
            if not item.source_id:
                unlinked_by_key.setdefault((item.title.casefold(), item.category.casefold()), []).append(item)

        for record in records:
            source_id = record.get('source_id')
            key = (record['title'].casefold(), record['category'].casefold())
            item = by_source_id.get(source_id) if source_id else None
            if item is None and unlinked_by_key.get(key):
                item = unlinked_by_key[key].pop(0)
            if item is None:
                item = MenuItem(title=record['title'], category=record['category'], price=record['price'])
                db.add(item)
                stats['inserted'] += 1
            else:
                stats['updated'] += 1

            item.source_id = source_id
            item.title = record['title'][:160]
            item.title_kk = record.get('title_kk', record['title'])[:160]
            item.category = record['category'][:80]
            item.category_kk = record.get('category_kk', record['category'])[:80]
            item.description = record.get('description', '')
            item.description_kk = record.get('description_kk', record.get('description', ''))
            item.price = record['price']
            item.image_path = local_image_path(record, image_files)
            item.source_image_url = record.get('source_image_url')
            item.is_active = bool(record.get('is_active', True))
            if item.image_path == DEFAULT_IMAGE_PATH:
                stats['without_image'] += 1
            if source_id:
                by_source_id[source_id] = item

        for item in current_items:
            if item.source_id and item.source_id not in imported_ids:
                item.is_active = False
            demo_values = DEMO_ITEMS.get((item.title.casefold(), item.category.casefold()))
            if item.source_id is None and demo_values:
                demo_description, demo_price = demo_values
                if item.description == demo_description and item.price == demo_price:
                    item.is_active = False

        db.commit()

    return {**stats, 'total': total}


def main():
    from parser import parse_menu

    records = parse_menu()
    result = seed_menu(records)
    print(
        'Импорт завершён: '
        f"добавлено {result['inserted']}, обновлено {result['updated']}, "
        f"без локального фото {result['without_image']}"
    )
    print(f"Всего записей меню: {result['total']}")


if __name__ == '__main__':
    main()