# Blog post outline: "How to scrape Next.js sites without your code breaking every redeploy"

Target keywords: *scrape Next.js*, *parse RSC flight payload*, *Next.js
hidden data*, *Next.js App Router scraping*, *self.__next_f.push*.

Platforms: Dev.to, Medium, Hashnode (cross-post the same piece to all
three with canonical-URL pointing at one primary home).

## Outline

1. **The problem, concretely** (hook)
   - Show a real `view-source:` snippet of a Next.js page: no visible
     `<table>` or clean `<div class="price">`, just a wall of
     `self.__next_f.push([...])` calls.
   - The naive first instinct: `response.text.split("push(")` and regex
     for a price-looking number. Show why that's fragile (thousands
     separators, currency symbols, multiple prices per page).

2. **Why `<table>`/CSS-selector scraping doesn't work here at all**
   - App Router pages often server-render to fairly generic markup;
     the *real* structured data lives in the Flight payload, not in
     semantic HTML you can `select()` your way through.
   - Reuse the HTML/JSON explainer analogy from the earlier LinkedIn post:
     HTML is the finished cake, this JSON is the recipe and the pantry —
     if you only have the recipe you can reconstruct the cake, but if you
     only have the cake (the rendered HTML) some ingredients are already
     mixed together and impossible to separate back out.

3. **What's actually in there**
   - Chunked rows: `id:json`, `id:T<hexLen>,<text>`, `id:I[...]`,
     `id:HL[...]`.
   - `$`-sigil references and why they exist (dedup repeated subtrees).
   - Screenshot/snippet of a real (anonymized) payload with refs
     highlighted.

4. **The naive index-chasing approach, and why it breaks**
   - `data[3]["children"][0][3]["props"]["price"]` works today.
   - Show (or describe) what changes on a redeploy: same data, different
     chunk id, different tree depth. The index chain now points at
     something else entirely, or throws.

5. **Search by shape, not by position**
   - Introduce `nextflight.extract(html).find_by_keys({"price", "title"})`.
   - Show the same lookup surviving a simulated "redeploy" (two payloads
     with the same data at different chunk ids/depths).

6. **Worked example**: pick one of e-commerce price scraping, job-board
   listing extraction, or SEO/`find_json_ld` auditing — walk through it
   end to end with real (or realistic synthetic) output.

7. **Honest caveats**
   - This is an undocumented, unversioned format. It can change.
   - Link to the README's maintenance-sustainability note and
     `next_version_hint()`/`parse_confidence()` as the "here's how you'd
     notice if it did" answer.

8. **Close** with install line and repo link.

## Distribution checklist
- [ ] Cross-post to Dev.to, Medium, Hashnode with correct canonical tag
- [ ] Share to r/Python and r/webscraping once live (not simultaneously
      with the Show HN post — space launches out over ~1-2 weeks so each
      gets its own attention)
- [ ] Link from the README's "Learn more" section once published
