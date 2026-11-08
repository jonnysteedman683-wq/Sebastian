## 2024-11-08 - Added CSP headers to generated HTML pages
**Vulnerability:** Missing defense-in-depth protection against XSS in generated HTML pages (`dashboard.py`, `timeline.py`).
**Learning:** Even when using string escaping like `html.escape` to neutralize untrusted inputs, it is a security best practice to add a strict Content-Security-Policy (CSP) header, which acts as a fallback to prevent malicious script execution in case of an escaping bypass or other injection vector.
**Prevention:** Implement a strict `<meta http-equiv="Content-Security-Policy" ...>` tag in all dynamically generated HTML pages to prevent XSS.
