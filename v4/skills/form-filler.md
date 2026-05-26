# Form Filler Skill

Fill out web forms using playwright-cli commands. Handles single-page and multi-page/wizard forms.
The browser is already connected — just use the commands below directly.

## Commands Reference

| Action | Command | Args |
|--------|---------|------|
| Navigate | `goto` | `["<url>"]` |
| See page | `snapshot` | `[]` |
| Click | `click` | `["<ref>"]` |
| Fill input | `fill` | `["<ref>", "<text>"]` |
| Select dropdown | `select` | `["<ref>", "<option_value>"]` |
| Check box | `check` | `["<ref>"]` |
| Uncheck box | `uncheck` | `["<ref>"]` |
| Press key | `press` | `["<ref>", "Enter"]` |
| Scroll | `scroll` | `["down"]` or `["up"]` |

## Single-Page Form

```
snapshot → fill each field → click Submit → snapshot (verify)
```

## Multi-Page / Wizard Form

Multi-page forms have Next/Continue/Step buttons. Handle them page by page:

```
snapshot
  → identify visible fields on THIS page
  → fill all visible fields
  → click Next/Continue
  → snapshot (new page loads)
  → fill visible fields on THIS page
  → click Next/Continue
  → ... repeat ...
  → click Submit on final page
  → snapshot (verify confirmation)
```

### Critical Rules for Multi-Page Forms

1. **Always snapshot after every page transition** — refs are INVALIDATED after navigation.
2. **Only fill fields you can SEE** — if a field isn't in the snapshot, it's on another page.
3. **Don't assume form structure** — always snapshot to discover what's on each page.
4. **Track your progress** — note which step/page you're on.
5. **Wait after clicking Next** — some forms load async. If snapshot shows loading, wait then re-snapshot.
6. **Handle validation errors** — if submit fails, snapshot to see error messages, fix them, retry.

## Filling Different Input Types

- **Text input**: `fill` with `["ref", "value"]`
- **Password**: same as text input, use `fill`
- **Dropdown/Select**: `select` with `["ref", "option_value"]`
- **Checkbox**: `check` or `uncheck` with `["ref"]`
- **Radio button**: `click` with `["ref"]` on the desired option
- **Date picker**: 
  1. First `click` the date input to focus/open it
  2. Try `fill` with `["ref", "YYYY-MM-DD"]` or `["ref", "MM/DD/YYYY"]` (match the format shown)
  3. If fill doesn't work (field rejects it), use `press` with `["ref", "Enter"]` after fill
  4. If a calendar popup appears, use `snapshot` to see it, then `click` the correct date
  5. After setting the date, `click` somewhere else or `press Tab` to close the picker
- **File upload**: not supported via playwright-cli in remote sessions
- **Textarea**: use `fill` same as text input

## Tips

- If a form has a "Show Password" toggle, ignore it.
- For address forms, fill fields in order: street → city → state → zip → country.
- If you see a CAPTCHA, report it to the user — you cannot solve it.
- After final submission, always snapshot to confirm success or catch errors.
