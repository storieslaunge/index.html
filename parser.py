import argparse
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen


MENU_URL = 'https://stories.kamiqr.com/menu/basic/list?utm_media=qr'
USER_AGENT = 'Mozilla/5.0 (compatible; StoriesMenuParser/1.0)'
REQUEST_TIMEOUT = 30
BESTSELLER_NAMES = {'хиты продаж', 'бестселлеры', 'best sellers'}
CATEGORY_ALIASES = {
    'авторские чаи (900 мл)': ('Авторский чай', 'Авторлық шай'),
    'авторские чаи (без сахара) (900 мл)': ('Авторский чай без сахара', 'Қантсыз авторлық шай'),
    'айс ти 400 мл': ('Айс-ти', 'Айс-ти'),
    'классическая пицца с сырными бортиками': ('Пицца с сырными бортиками', 'Ірімшік бортигі бар пицца'),
    'кофе (350 мл)': ('Кофе', 'Кофе'),
    'лимонады (1л)': ('Лимонады', 'Лимонадтар'),
    'листовые чаи (900 мл)': ('Листовой чай', 'Жапырақты шай'),
    'милк-шейки (350 мл)': ('Молочные коктейли', 'Сүтті коктейльдер'),
    'свежевыжатые соки (250 мл)': ('Свежевыжатые соки', 'Жаңа сығылған шырындар'),
    'смузи (350 мл)': ('Смузи', 'Смузи'),
    'смуси (350 мл)': ('Смузи', 'Смузи'),
    'tealim (350 мл)': ('Чай Tea Lim', 'Tea Lim шайы'),
    'фирменные бургеры': ('Бургеры', 'Бургерлер'),
    'десерты': ('Десерты', 'Тәттілер'),
    'ice кофе': ('Кофе со льдом', 'Мұзды кофе'),
}


class InlineScriptParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts = []
        self._current_script = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'script' and not attributes.get('src'):
            self._current_script = []
            self.scripts.append(self._current_script)

    def handle_data(self, data):
        if self._current_script is not None:
            self._current_script.append(data)

    def handle_endtag(self, tag):
        if tag == 'script':
            self._current_script = None


def fetch_menu_html(url=MENU_URL):
    request = Request(url, headers={'User-Agent': USER_AGENT})
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.read().decode('utf-8', 'replace')


def extract_menu_state(html):
    parser = InlineScriptParser()
    parser.feed(html)
    decoder = json.JSONDecoder()

    for parts in parser.scripts:
        script = ''.join(parts)
        match = re.search(r'window\.__data__\s*=\s*', script)
        if not match:
            continue
        try:
            state, _ = decoder.raw_decode(script, match.end())
        except json.JSONDecodeError as error:
            raise ValueError(f'Не удалось разобрать встроенные данные меню: {error}') from error
        if isinstance(state, dict) and isinstance(state.get('data'), dict):
            return state

    raise ValueError('В HTML не найден window.__data__ с данными меню')


def localized(value):
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ''
    for language in ('RU', 'EN', 'KZ'):
        text = value.get(language)
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ''


def canonical_category(value):
    russian = localized(value)
    kazakh = localized(value.get('KZ')) if isinstance(value, dict) else ''
    normalized = ' '.join(russian.casefold().split())
    alias = CATEGORY_ALIASES.get(normalized)
    if alias:
        return alias
    russian = russian[:1].upper() + russian[1:].lower() if russian else ''
    kazakh = kazakh[:1].upper() + kazakh[1:].lower() if kazakh else russian
    return russian, kazakh


def parse_price(item):
    raw_price = item.get('prices', {}).get('primary', {}).get('price')
    if raw_price is None:
        return None
    try:
        return Decimal(str(raw_price)) / Decimal('100')
    except (InvalidOperation, TypeError, ValueError):
        return None


def parse_menu_state(state):
    data = state['data']
    categories = data.get('categories', {})
    items = data.get('items', {})
    sections = data.get('sections', {})
    tree = data.get('tree', {}).get('sections', [])
    assets_base = state.get('assetsUrl', '').rstrip('/')
    records_by_id = {}

    for section_node in tree:
        section = sections.get(section_node.get('sectionId'), {})
        if section.get('isHidden'):
            continue
        for category_node in section_node.get('categories', []):
            category = categories.get(category_node.get('categoryId'), {})
            if not category or category.get('isHidden'):
                continue
            category_title, category_title_kk = canonical_category(category.get('name'))
            if not category_title:
                continue
            normalized_category = category_title.casefold()

            for item_ref in category_node.get('items') or []:
                item_id = item_ref.get('itemId')
                item = items.get(item_id)
                if not item:
                    continue

                title = localized(item.get('name'))
                title_kk = localized((item.get('name') or {}).get('KZ'))
                price = parse_price(item)
                if not title or price is None:
                    continue

                specs = item.get('specs') or {}
                description = localized(specs.get('shortDesc')) or localized(specs.get('fullDesc'))
                description_kk = localized((specs.get('shortDesc') or {}).get('KZ')) or localized((specs.get('fullDesc') or {}).get('KZ'))
                source_image = item.get('mainImg') or None
                source_image_url = (
                    f'{assets_base}/menuItems/{quote(source_image)}'
                    if assets_base and source_image else None
                )
                record = {
                    'source_id': item_id,
                    'title': title[:160],
                    'title_kk': (title_kk or title)[:160],
                    'description': description,
                    'description_kk': description_kk or description,
                    'category': category_title[:80],
                    'category_kk': (category_title_kk or category_title)[:80],
                    'price': price,
                    'source_image': source_image,
                    'source_image_url': source_image_url,
                    'is_active': not item.get('isHidden', False) and not item.get('isOff', False),
                }

                priority = 1 if normalized_category in BESTSELLER_NAMES else 0
                previous = records_by_id.get(item_id)
                if previous is None or priority < previous[0]:
                    records_by_id[item_id] = (priority, record)

    records = [record for _, record in records_by_id.values()]
    catalog_path = Path(__file__).with_name('catalog_kk.json')
    if catalog_path.is_file():
        catalog = json.loads(catalog_path.read_text(encoding='utf-8')).get('items', {})
        for record in records:
            localized_record = catalog.get(record['source_id'], {})
            for field in ('title_kk', 'description_kk'):
                if field in localized_record:
                    record[field] = localized_record[field]
    return records


def parse_menu(url=MENU_URL):
    html = fetch_menu_html(url)
    return parse_menu_state(extract_menu_state(html))


def main(argv=None):
    argument_parser = argparse.ArgumentParser(description='Импорт меню Stories в SQLite')
    argument_parser.add_argument('--dry-run', action='store_true', help='Проверить меню без записи в базу')
    argument_parser.add_argument('--url', default=MENU_URL, help='URL страницы меню Stories')
    args = argument_parser.parse_args(argv)

    try:
        records = parse_menu(args.url)
    except Exception as error:
        print(f'Не удалось получить меню: {error}', file=sys.stderr)
        return 1

    if args.dry_run:
        categories = sorted({record['category'] for record in records})
        print(f'Найдено блюд: {len(records)}')
        print(f'Категорий: {len(categories)}')
        for record in records[:5]:
            print(f"{record['category']} / {record['category_kk']} | {record['title']} / {record['title_kk']} | {record['price']} ₸ | {record['source_image'] or 'без фото'}")
        return 0

    from seed_db import seed_menu

    result = seed_menu(records)
    print(
        'Импорт завершён: '
        f"добавлено {result['inserted']}, обновлено {result['updated']}, "
        f"без локального фото {result['without_image']}"
    )
    print(f"Всего записей меню: {result['total']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())