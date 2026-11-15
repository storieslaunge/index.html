import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


START_URL = 'https://stories.kamiqr.com/menu/basic?utm_media=qr'
MENU_PATH = '/menu/basic/list'
OUTPUT_DIR = Path(__file__).resolve().parent / 'pictures' / 'stories_kamiqr'
USER_AGENT = 'Mozilla/5.0 (compatible; StoriesMenuImageDownloader/1.0)'
TIMEOUT_SECONDS = 30
MAX_WORKERS = 8
IMAGE_EXTENSIONS = {'.avif', '.gif', '.jpeg', '.jpg', '.png', '.svg', '.webp'}
ASSET_HOST = 'kamigroup.fra1.cdn.digitaloceanspaces.com'


class PageAssets(HTMLParser):
  def __init__(self):
    super().__init__(convert_charrefs=True)
    self.links = []
    self.images = []
    self.styles = []
    self._in_style = False

  def handle_starttag(self, tag, attrs):
    attributes = dict(attrs)
    if tag == 'a' and attributes.get('href'):
      self.links.append(attributes['href'])
    if tag in {'img', 'source'}:
      for name in ('src', 'data-src', 'data-lazy-src', 'data-original', 'poster'):
        if attributes.get(name):
          self.images.append(attributes[name])
      for name in ('srcset', 'data-srcset'):
        for candidate in attributes.get(name, '').split(','):
          value = candidate.strip().split(' ', 1)[0]
          if value:
            self.images.append(value)
    if attributes.get('style'):
      self.styles.append(attributes['style'])
    if tag == 'style':
      self._in_style = True

  def handle_data(self, data):
    if self._in_style:
      self.styles.append(data)

  def handle_endtag(self, tag):
    if tag == 'style':
      self._in_style = False


def get_page(url):
  request = Request(url, headers={'User-Agent': USER_AGENT})
  with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
    return response.geturl(), response.read().decode('utf-8', 'replace')


def add_asset_url(urls, base, folder, filename):
  name = filename.strip()
  if not name or PurePosixPath(name).name != name:
    return
  if PurePosixPath(name).suffix.lower() not in IMAGE_EXTENSIONS:
    return
  urls.add(urljoin(f"{base.rstrip('/')}/{folder}/", name))


def collect_page_images(page_url, html, urls):
  parser = PageAssets()
  parser.feed(html)

  assets_match = re.search(r'"assetsUrl"\s*:\s*"(https?://[^"\\]+)"', html)
  assets_base = assets_match.group(1).rstrip('/') if assets_match else None

  for candidate in parser.images:
    image_url = urljoin(page_url, candidate)
    parsed = urlparse(image_url)
    if parsed.scheme in {'http', 'https'} and PurePosixPath(parsed.path).suffix.lower() in IMAGE_EXTENSIONS:
      urls.add(image_url)

  for style in parser.styles:
    for candidate in re.findall(r'url\(["\']?([^"\')]+)', style, re.I):
      image_url = urljoin(page_url, candidate.strip())
      parsed = urlparse(image_url)
      if parsed.scheme in {'http', 'https'} and PurePosixPath(parsed.path).suffix.lower() in IMAGE_EXTENSIONS:
        urls.add(image_url)

  if not assets_base:
    return parser.links

  for filename in re.findall(r'"mainImg"\s*:\s*"([^"\\]+)"', html):
    add_asset_url(urls, assets_base, 'menuItems', filename)

  image_names = re.findall(r'"img"\s*:\s*"([^"\\]+)"', html)
  slides_match = re.search(r'"slides"\s*:\s*\[(.*?)\]', html, re.S)
  slide_names = set(
    re.findall(r'"img"\s*:\s*"([^"\\]+)"', slides_match.group(1))
  ) if slides_match else set()
  for filename in image_names:
    folder = 'menuSlides' if filename in slide_names else 'menuSections'
    add_asset_url(urls, assets_base, folder, filename)

  for filename in re.findall(r'"backgroundImg"\s*:\s*"([^"\\]+)"', html):
    add_asset_url(urls, assets_base, 'menuOrgAttrsBackgrounds', filename)

  return parser.links


def image_path(url):
  parsed = urlparse(url)
  if parsed.hostname == ASSET_HOST and '/kami/prod/' in parsed.path:
    relative = parsed.path.split('/kami/prod/', 1)[1]
    return OUTPUT_DIR / Path(*PurePosixPath(relative).parts)
  if parsed.hostname == 'stories.kamiqr.com':
    return OUTPUT_DIR / 'site' / Path(PurePosixPath(parsed.path).name)
  return OUTPUT_DIR / 'external' / Path(PurePosixPath(parsed.path).name)


def is_image_content(content):
  if content.startswith((b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n', b'GIF87a', b'GIF89a')):
    return True
  if len(content) >= 12 and content[:4] == b'RIFF' and content[8:12] == b'WEBP':
    return True
  if len(content) >= 12 and content[4:8] == b'ftyp' and any(brand in content[:32] for brand in (b'avif', b'avis')):
    return True
  prefix = content[:512].lstrip(b'\xef\xbb\xbf\x00\t\r\n ')
  return prefix.startswith(b'<svg') or (prefix.startswith(b'<?xml') and b'<svg' in prefix.lower())


def download_image(url):
  parsed = urlparse(url)
  if parsed.hostname not in {ASSET_HOST, 'stories.kamiqr.com'}:
    return 'skipped', url, 'внешний домен не входит в список источников'

  destination = image_path(url)
  if destination.exists() and destination.stat().st_size > 0:
    return 'existing', url, str(destination.relative_to(OUTPUT_DIR))

  request = Request(url, headers={'User-Agent': USER_AGENT, 'Referer': START_URL})
  try:
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
      content = response.read()
      content_type = response.headers.get_content_type()
    if not is_image_content(content):
      return 'failed', url, f'неверный формат файла: {content_type}'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return 'downloaded', url, str(destination.relative_to(OUTPUT_DIR))
  except (HTTPError, URLError, TimeoutError, OSError) as error:
    return 'failed', url, str(error)


def main():
  page_urls = [START_URL]
  seen_pages = set()
  image_urls = set()

  while page_urls:
    page_url = page_urls.pop(0)
    if page_url in seen_pages:
      continue
    seen_pages.add(page_url)
    final_url, html = get_page(page_url)
    links = collect_page_images(final_url, html, image_urls)
    for link in links:
      target = urljoin(final_url, link)
      parsed = urlparse(target)
      if parsed.hostname == 'stories.kamiqr.com' and parsed.path.rstrip('/') == MENU_PATH:
        if target not in seen_pages and target not in page_urls:
          page_urls.append(target)

  if not image_urls:
    print('На страницах не найдены изображения.', file=sys.stderr)
    return 1

  OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
  results = []
  with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = [executor.submit(download_image, url) for url in sorted(image_urls)]
    for future in as_completed(futures):
      results.append(future.result())

  downloaded = sum(status == 'downloaded' for status, _, _ in results)
  existing = sum(status == 'existing' for status, _, _ in results)
  failed = [(url, detail) for status, url, detail in results if status == 'failed']
  skipped = sum(status == 'skipped' for status, _, _ in results)

  print(f'Найдено URL изображений: {len(image_urls)}')
  print(f'Скачано: {downloaded}; уже было: {existing}; пропущено: {skipped}; ошибок: {len(failed)}')
  print(f'Папка: {OUTPUT_DIR}')
  for url, error in failed:
    print(f'Ошибка: {url} ({error})', file=sys.stderr)
  return 1 if failed else 0


if __name__ == '__main__':
  raise SystemExit(main())