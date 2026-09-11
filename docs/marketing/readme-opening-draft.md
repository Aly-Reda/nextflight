# Alternative README opening — problem-first, conversion-focused

This is a draft alternative to the current README opening, leading
harder with the *problem* before naming the library, per the "README as
a conversion tool" idea (people decide whether to try a library within
seconds of landing on the repo). Swap this in for the top of README.md
if you want to test it.

---

# nextflight

Your scraper works today. It'll break the next time this site redeploys.

That's not a bug in your code — it's how Next.js App Router pages ship
data. There's no `<table>` or `class="price"` to select; the real data is
buried in `self.__next_f.push([...])` script blocks, chunked and
cross-referenced with `$`-sigils Next.js uses to dedupe repeated parts of
the page. Most scrapers end up hardcoding something like
`data[3]["children"][0][3]["props"]["price"]` to reach into it — and that
index chain silently points at something else (or throws) the moment the
component tree reshuffles on a redeploy.

`nextflight` parses that wire format properly and lets you search for the
*shape* of data you want instead of an index path:

```python
from nextflight import extract

page = extract(response.text)                          # any Next.js 13+ App Router page
listing = page.find_by_keys({"price", "title"})         # survives redeploys; index chains don't
```

```bash
pip install nextflight   # stdlib only -- nothing else required
```

No per-site config, no required dependencies, works with Scrapy/requests/
httpx/the stdlib alone. See [Comparison with other tools](#comparison-with-other-tools)
below for how this differs from `njsparser`, the Burp Suite RSC parser
extension, and the `rsc-parser.vercel.app` web tool.

---

(continue with the existing "Install" section onward)
