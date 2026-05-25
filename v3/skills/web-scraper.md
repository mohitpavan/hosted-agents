# Web Scraper Skill

Extract data from web pages using playwright-cli commands.

## Prerequisites (MUST do before any command below)

1. Call `browser_automation_preview___create_session` to get a CDP URL.
2. Call `run_browser` with command="connect", args=["<cdp_url>"]
3. ONLY THEN proceed with goto/snapshot/eval below.

## Commands Reference

| Action | Command | Args |
|--------|---------|------|
| Navigate | `goto` | `["<url>"]` |
| See page structure | `snapshot` | `[]` |
| Get page title | `eval` | `["document.title"]` |
| Get text content | `eval` | `["document.body.innerText"]` |
| Get specific element | `eval` | `["document.querySelector('<selector>').textContent"]` |
| Get all links | `eval` | `["JSON.stringify([...document.querySelectorAll('a')].map(a => ({text: a.textContent.trim(), href: a.href})))"]` |
| Get table data | `eval` | `["JSON.stringify([...document.querySelectorAll('table tr')].map(r => [...r.cells].map(c => c.textContent.trim())))"]` |
| Get meta tags | `eval` | `["JSON.stringify({title: document.title, description: document.querySelector('meta[name=description]')?.content, keywords: document.querySelector('meta[name=keywords]')?.content})"]` |
| Screenshot | `screenshot` | `[]` |
| Scroll down | `scroll` | `["down"]` |
| Click link | `click` | `["<ref>"]` |
| Go back | `go-back` | `[]` |

## Scraping Workflow

```
create_session → get cdp_url
  → connect <cdp_url>
  → goto <url>
  → snapshot (understand page structure)
  → eval (extract the specific data needed)
  → report results to user
```

## Multi-Page Scraping

For paginated content (search results, product listings, etc.):

```
goto <url>
  → snapshot
  → eval (extract data from this page)
  → find Next/pagination link in snapshot
  → click <next_ref>
  → snapshot
  → eval (extract data from next page)
  → ... repeat until done ...
```

## Extracting Structured Data

Use `eval` with JavaScript to extract exactly what you need:

```javascript
// Get all product cards
JSON.stringify([...document.querySelectorAll('.product-card')].map(el => ({
  name: el.querySelector('.title')?.textContent?.trim(),
  price: el.querySelector('.price')?.textContent?.trim(),
  link: el.querySelector('a')?.href
})))

// Get article content
document.querySelector('article')?.innerText

// Get all headings
JSON.stringify([...document.querySelectorAll('h1,h2,h3')].map(h => h.textContent.trim()))
```

## Tips

- **Start with `snapshot`** to understand page layout before using `eval`.
- **Use `eval` for data extraction** — it's faster and more precise than parsing snapshot text.
- **Handle infinite scroll**: `scroll down` → `eval` → check if new content appeared → repeat.
- **For SPAs**: after navigation, wait a moment then `snapshot` — content may load async.
- **Long pages**: combine `scroll down` + `eval` to access content below the fold.
- **If eval returns null/undefined**: the selector is wrong. Use `snapshot` to find the right elements.
- **Rate limiting**: if you get blocked, report to user — don't retry aggressively.
- **Cookie banners**: if a popup blocks content, `snapshot` to find dismiss button, `click` it.
