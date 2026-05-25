# Form Filler Skill

Fill out web forms using playwright-cli commands. Handles single-page and multi-page/wizard forms.

## Prerequisites (MUST do before any command below)

1. Call `browser_automation_preview___create_session` to get a CDP URL.
2. Call `run_browser` with command="connect", args=["<cdp_url>"]
3. ONLY THEN proceed with goto/snapshot/fill below.

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
- **Date picker**: try `fill` first; if it doesn't work, `click` to open picker then navigate
- **File upload**: not supported via playwright-cli in remote sessions
- **Textarea**: use `fill` same as text input

## Tips

- If a form has a "Show Password" toggle, ignore it.
- For address forms, fill fields in order: street → city → state → zip → country.
- If you see a CAPTCHA, report it to the user — you cannot solve it.
- After final submission, always snapshot to confirm success or catch errors.
