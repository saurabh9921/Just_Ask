# Content scripts

**No content scripts are injected in the MVP.**

ERP Insight AI works entirely from the side panel and the background service worker.
Injecting scripts into web pages would require broad host permissions (`<all_urls>`)
and would expose the extension to hostile page content, so it is deliberately omitted
(principle of least privilege).

If a future feature needs page context (for example, reading the ERP record currently
open in a tab), add it here with:

- the narrowest possible `matches` pattern in `manifest.json`, scoped to approved ERP hosts;
- no access to tokens (`chrome.storage.session` is restricted to trusted contexts);
- messages to the background worker validated like any other untrusted input;
- a security review, documented in `docs/security.md`.
