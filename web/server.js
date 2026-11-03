// TechShorts server: fetches tech RSS/Atom feeds, normalizes to short cards, serves them + the static UI.
// No dependencies; needs Node 18+.
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';

const PORT = process.env.PORT || 3000;
const CACHE_MS = 15 * 60 * 1000;
const SUMMARY_WORDS = 60;
const PUBLIC = join(fileURLToPath(new URL('.', import.meta.url)), 'public');

const FEEDS = [
  // Top
  { source: 'TechCrunch', category: 'Top', url: 'https://techcrunch.com/feed/' },
  { source: 'The Verge', category: 'Top', url: 'https://www.theverge.com/rss/index.xml' },
  { source: 'Ars Technica', category: 'Top', url: 'https://feeds.arstechnica.com/arstechnica/index' },
  { source: 'Wired', category: 'Top', url: 'https://www.wired.com/feed/rss' },
  { source: 'Hacker News', category: 'Top', url: 'https://hnrss.org/frontpage?points=150' },
  // AI
  { source: 'VentureBeat AI', category: 'AI', url: 'https://venturebeat.com/category/ai/feed/' },
  { source: 'MIT Tech Review', category: 'AI', url: 'https://www.technologyreview.com/topic/artificial-intelligence/feed' },
  { source: 'Hugging Face', category: 'AI', url: 'https://huggingface.co/blog/feed.xml' },
  { source: 'OpenAI', category: 'AI', url: 'https://openai.com/news/rss.xml' },
  // Startups
  { source: 'TechCrunch Startups', category: 'Startups', url: 'https://techcrunch.com/category/startups/feed/' },
  { source: 'Product Hunt', category: 'Startups', url: 'https://www.producthunt.com/feed' },
  // Dev tools
  { source: 'GitHub Blog', category: 'Dev', url: 'https://github.blog/feed/' },
  { source: 'Cloudflare', category: 'Dev', url: 'https://blog.cloudflare.com/rss/' },
  { source: 'DEV Community', category: 'Dev', url: 'https://dev.to/feed' },
  // Blogs worth reading
  { source: 'Simon Willison', category: 'Blogs', url: 'https://simonwillison.net/atom/everything/' },
  { source: 'Pragmatic Engineer', category: 'Blogs', url: 'https://newsletter.pragmaticengineer.com/feed' },
  { source: 'Julia Evans', category: 'Blogs', url: 'https://jvns.ca/atom.xml' },
];
const CATEGORIES = ['Top', 'AI', 'Startups', 'Dev', 'Blogs'];
const PER_FEED = 12;

const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', hellip: '…', mdash: '—', ndash: '–', rsquo: '’', lsquo: '‘', rdquo: '”', ldquo: '“' };
const decode = (s) =>
  s.replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(+n))
    .replace(/&#x([0-9a-f]+);/gi, (_, n) => String.fromCodePoint(parseInt(n, 16)))
    .replace(/&([a-z]+);/gi, (m, n) => ENTITIES[n.toLowerCase()] ?? m);
const unwrap = (s) => s.replace(/^\s*<!\[CDATA\[([\s\S]*?)\]\]>\s*$/, '$1');
const text = (html) => decode(html.replace(/<(script|style)[\s\S]*?<\/\1>/gi, '').replace(/<[^>]+>/g, ' ')).replace(/\s+/g, ' ').trim();

const tag = (block, name) => {
  const m = block.match(new RegExp(`<${name}(?:\\s[^>]*)?>([\\s\\S]*?)</${name}>`, 'i'));
  return m ? unwrap(m[1]) : '';
};

function summarize(html, title) {
  let t = text(html);
  t = t.replace(/\s*(The post .* appeared first on .*|Continue reading.*|Read more.*|Comments)$/i, '').trim();
  if (!t || t.toLowerCase() === title.toLowerCase()) return '';
  const words = t.split(' ');
  if (words.length <= SUMMARY_WORDS) return t;
  const cut = words.slice(0, SUMMARY_WORDS).join(' ');
  const lastStop = Math.max(cut.lastIndexOf('. '), cut.lastIndexOf('? '), cut.lastIndexOf('! '));
  return lastStop > cut.length * 0.5 ? cut.slice(0, lastStop + 1) : cut.replace(/[,;:\s]+$/, '') + '…';
}

function parseFeed(xml, source) {
  const blocks = xml.match(/<(item|entry)[\s>][\s\S]*?<\/\1>/gi) || [];
  return blocks.map((b) => {
    const title = text(tag(b, 'title'));
    const link = (b.match(/<link[^>]*rel=["']alternate["'][^>]*href=["']([^"']+)/i) || b.match(/<link[^>]*href=["']([^"']+)/i) || [])[1] || text(tag(b, 'link'));
    const rawBody = tag(b, 'content:encoded') || tag(b, 'content') || tag(b, 'description') || tag(b, 'summary');
    const date = new Date(text(tag(b, 'pubDate') || tag(b, 'published') || tag(b, 'updated') || tag(b, 'dc:date')));
    const image =
      (b.match(/<media:(?:content|thumbnail)[^>]*url=["']([^"']+)/i) || [])[1] ||
      (b.match(/<enclosure[^>]*url=["']([^"']+)["'][^>]*type=["']image/i) || [])[1] ||
      (decode(rawBody).match(/<img[^>]*src=["']([^"']+)/i) || [])[1] || null;
    return { id: link, title, summary: summarize(decode(rawBody), title), url: link, image, source, publishedAt: isNaN(date) ? null : date.toISOString() };
  }).filter((a) => a.title && a.url && a.publishedAt);
}

let cache = { at: 0, articles: [] };
let inflight = null;

async function refresh() {
  const results = await Promise.allSettled(FEEDS.map(async (f) => {
    const res = await fetch(f.url, { headers: { 'user-agent': 'TechShorts/0.1' }, signal: AbortSignal.timeout(10_000) });
    if (!res.ok) throw new Error(`${f.source}: HTTP ${res.status}`);
    return parseFeed(await res.text(), f.source).slice(0, PER_FEED).map((a) => ({ ...a, category: f.category }));
  }));
  results.forEach((r, i) => r.status === 'rejected' && console.warn('feed failed:', FEEDS[i].source, r.reason.message));
  const seen = new Set();
  const articles = results.flatMap((r) => (r.status === 'fulfilled' ? r.value : []))
    .filter((a) => !seen.has(a.url) && seen.add(a.url))
    .sort((a, b) => b.publishedAt.localeCompare(a.publishedAt));
  if (articles.length) cache = { at: Date.now(), articles };
  return cache.articles;
}

async function getArticles() {
  if (Date.now() - cache.at < CACHE_MS) return cache.articles;
  inflight ??= refresh().finally(() => (inflight = null));
  return inflight;
}

const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png' };

http.createServer(async (req, res) => {
  const { pathname, searchParams } = new URL(req.url, 'http://x');
  try {
    if (pathname === '/api/news') {
      let list = await getArticles();
      const category = searchParams.get('category');
      if (category) list = list.filter((a) => a.category === category);
      res.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'public, max-age=300' });
      return res.end(JSON.stringify({ updatedAt: new Date(cache.at).toISOString(), categories: CATEGORIES, articles: list.slice(0, 60) }));
    }
    const file = join(PUBLIC, normalize(pathname === '/' ? '/index.html' : pathname));
    if (!file.startsWith(PUBLIC)) { res.writeHead(403); return res.end(); }
    res.writeHead(200, { 'content-type': TYPES[extname(file)] || 'application/octet-stream' });
    res.end(await readFile(file));
  } catch (e) {
    res.writeHead(e.code === 'ENOENT' ? 404 : 500); res.end(e.code === 'ENOENT' ? 'Not found' : 'Server error');
  }
}).listen(PORT, () => console.log(`TechShorts running at http://localhost:${PORT}`));
