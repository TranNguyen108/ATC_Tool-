"""
form_detect.py — Nhận diện comment form đa layout.

Chiến lược phát hiện form (theo thứ tự ưu tiên):
  Layer 1: WordPress standard (#commentform)
  Layer 2: form[id*="comment" i] / form[class*="comment" i] / form trong #respond
  Layer 3: Heuristic — form có textarea + input email
  Layer 4: Fallback — form cuối cùng có textarea

Trả về FormResult chứa locators của từng field (None nếu field không tồn tại trên trang).
Chỉ điền field thực sự hiện diện — không tạo thêm dữ liệu.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Optional
from playwright.async_api import Page, Locator

log = logging.getLogger("atc.form_detect")


@dataclass
class FormResult:
    found: bool = False
    form: Optional[Locator] = None
    # Các field — None nếu không tồn tại trên trang
    f_name: Optional[Locator] = None
    f_email: Optional[Locator] = None
    f_url: Optional[Locator] = None
    f_comment: Optional[Locator] = None
    f_submit: Optional[Locator] = None
    f_rating: Optional[Locator] = None
    rating_type: str = "none"  # stars_select, stars_radio, stars_click, select_text
    has_captcha: bool = False
    strategy: str = "none"


async def _is_visible(locator: Locator) -> bool:
    """Kiểm tra locator có tồn tại và hiển thị không.
    timeout=200ms — đủ cho DOM query, tránh tích luỹ chậm (hàng chục selector × 500ms = treo).
    """
    try:
        count = await locator.count()
        if count == 0:
            return False
        return await locator.first.is_visible(timeout=200)
    except Exception:
        return False


async def _exists_in_dom(locator: Locator) -> bool:
    """Kiểm tra locator tồn tại trong DOM (kể cả hidden)."""
    try:
        return await locator.count() > 0
    except Exception:
        return False


async def _first_visible(locators: list[Locator]) -> Optional[Locator]:
    """Trả về locator đầu tiên visible trong danh sách."""
    for loc in locators:
        if await _is_visible(loc):
            return loc.first
    return None


async def _find_field_in_form(form: Locator, selectors: list[str]) -> Optional[Locator]:
    """Tìm field đầu tiên match selector trong form đã cho."""
    combined_sel = ", ".join(selectors)
    loc = form.locator(combined_sel)
    if await _is_visible(loc):
        return loc.first
    return None


async def _find_rating(page: Page, form: Locator) -> tuple[Optional[Locator], str]:
    """Phát hiện rating elements (stars, select, radio) trong form hoặc trang."""
    # 1. Select dropdown rating (WooCommerce)
    select_sels = [
        "select#rating",
        "select[name*='rating' i]",
        "select[name*='review' i]",
        "select[id*='rating' i]",
        ".comment-form-rating select",
    ]
    combined_select = ", ".join(select_sels)
    for scope in [form, page]:
        loc = scope.locator(combined_select)
        if await _is_visible(loc):
            return loc.first, "stars_select"

    # 2. Radio buttons rating
    radio_sels = [
        "input[type='radio'][name*='rating' i]",
        "input[type='radio'][name*='review' i]",
        "input[type='radio'][name*='star' i]",
    ]
    combined_radio = ", ".join(radio_sels)
    for scope in [form, page]:
        loc = scope.locator(combined_radio)
        cnt = await loc.count()
        if cnt > 0:
            return loc.nth(cnt - 1), "stars_radio"

    # 3. Clickable star elements (WooCommerce .stars, custom widgets)
    star_sels = [
        ".comment-form-rating .stars a",
        ".comment-form-rating .stars span",
        "p.stars a",
        ".star-rating a",
        "[data-rating]",
        ".rating-star",
        "label[for*='rating' i]",
        "label[for*='star' i]",
    ]
    combined_star = ", ".join(star_sels)
    for scope in [page, form]:
        loc = scope.locator(combined_star)
        cnt = await loc.count()
        if cnt > 0:
            return loc.nth(cnt - 1), "stars_click"

    # 4. Select với text options (tốt, rất tốt, excellent, ...)
    for scope in [form, page]:
        select_all = scope.locator("select")
        count = await select_all.count()
        for i in range(min(count, 5)):
            sel_el = select_all.nth(i)
            try:
                has_quality = await sel_el.evaluate("""el => {
                    const opts = Array.from(el.options).map(o => o.text.toLowerCase());
                    const qw = ['tốt','rất tốt','xuất sắc','tuyệt vời','good','very good',
                                 'excellent','great','best','5 stars','4 stars','5 sao','4 sao'];
                    return opts.some(o => qw.some(q => o.includes(q)));
                }""")
                if has_quality:
                    return sel_el, "select_text"
            except Exception:
                pass

    return None, "none"


async def _extract_fields(page: Page, form: Locator) -> dict:
    """Trích xuất các field từ form đã tìm được."""
    name_sels = [
        "input[name*='author' i]",
        "input[name*='name' i]:not([name*='email' i])",
        "input[placeholder*='name' i]",
        "input[placeholder*='tên' i]",
        "input[id*='author' i]",
        "input[id*='name' i]:not([id*='email' i])",
    ]
    email_sels = [
        "input[type='email']",
        "input[name*='email' i]",
        "input[id*='email' i]",
        "input[placeholder*='email' i]",
    ]
    url_sels = [
        "input[name*='url' i]",
        "input[name*='website' i]",
        "input[name*='web' i]",
        "input[id*='url' i]",
        "input[id*='website' i]",
        "input[type='url']",
        "input[placeholder*='website' i]",
        "input[placeholder*='http' i]",
    ]
    comment_sels = [
        "textarea[name*='comment' i]",
        "textarea[id*='comment' i]",
        "textarea[name*='message' i]",
        "textarea[name*='content' i]",
        "textarea",  # fallback: textarea đầu tiên
    ]
    submit_sels = [
        "input[type='submit']",
        "button[type='submit']",
        "button:has-text('Post Comment')",
        "button:has-text('Submit')",
        "button:has-text('Comment')",
        "button:has-text('Send')",
        "input[value*='comment' i]",
        "input[value*='submit' i]",
        "button",  # fallback
    ]
    # Selector dùng cho fallback page-level (bỏ "button" quá rộng)
    submit_sels_page = [
        "input[type='submit']",
        "button[type='submit']",
        "button:has-text('Post Comment')",
        "button:has-text('Submit')",
        "button:has-text('Comment')",
        "button:has-text('Send')",
        "button:has-text('Đăng')",
        "button:has-text('Gửi')",
        "button:has-text('Bình luận')",
        "input[value*='comment' i]",
        "input[value*='submit' i]",
    ]

    submit_field = await _find_field_in_form(form, submit_sels)
    if submit_field is None:
        # Fallback: nút submit có thể nằm ngoài thẻ <form> (sibling element)
        submit_field = await _find_field_in_form(page, submit_sels_page)
        if submit_field is not None:
            log.debug("submit found outside <form> via page-level fallback")

    return {
        "name":    await _find_field_in_form(form, name_sels),
        "email":   await _find_field_in_form(form, email_sels),
        "url":     await _find_field_in_form(form, url_sels),
        "comment": await _find_field_in_form(form, comment_sels),
        "submit":  submit_field,
    }


async def _has_captcha(page: Page) -> bool:
    """Phát hiện các loại captcha phổ biến."""
    captcha_sels = [
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        "iframe[src*='turnstile']",
        ".g-recaptcha",
        ".h-captcha",
        "[data-sitekey]",
        "iframe[title*='captcha' i]",
        "#captcha",
        "[id*='captcha' i]",
        "[class*='captcha' i]",
    ]
    combined_sel = ", ".join(captcha_sels)
    try:
        loc = page.locator(combined_sel)
        if await loc.count() > 0:
            return True
    except Exception:
        pass
    return False


# ─────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────

async def find_comment_form(page: Page) -> FormResult:
    """
    Tìm comment form trên trang theo 4 layer.
    Trả về FormResult với các Locator field (None nếu field không tồn tại).
    """
    result = FormResult()
    form: Optional[Locator] = None

    # ── Layer 1: WordPress standard (#commentform) ──────────
    wp_form = page.locator("#commentform")
    # Thử visible trước, nếu không thì check DOM (form có thể vừa reveal)
    if await _is_visible(wp_form):
        form = wp_form.first
        result.strategy = "wordpress_standard"
    elif await _exists_in_dom(wp_form):
        # Form tồn tại nhưng có thể bị hidden → thử JS reveal
        try:
            await page.evaluate("""
                const f = document.querySelector('#commentform');
                if (f) {
                    f.style.display = '';
                    f.style.visibility = 'visible';
                    f.style.opacity = '1';
                    // Mở cả parent respond section nếu có
                    const r = document.querySelector('#respond');
                    if (r) { r.style.display = ''; r.style.visibility = 'visible'; }
                }
            """)
        except Exception:
            pass
        if await _is_visible(wp_form):
            form = wp_form.first
            result.strategy = "wordpress_standard_revealed"
            log.debug("Layer 1: revealed hidden #commentform")

    # ── Layer 2: form id/class chứa "comment" + form trong #respond ──
    if form is None:
        candidates_sels = [
            "form[id*='comment' i]",
            "form[class*='comment' i]",
            "form[id*='respond' i]",
            "#respond form",
            ".comment-respond form",
            ".comments-area form",
            "form[action*='comment' i]",
            "form[class*='review' i]",
            "form[id*='review' i]",
            ".review-form form",
            "#review_form",
            "#review_form_wrapper form",
            ".woocommerce-Reviews form",
        ]
        combined_sel = ", ".join(candidates_sels)
        combined_cand = page.locator(combined_sel)
        
        if await _is_visible(combined_cand):
            form = combined_cand.first
            result.strategy = "id/class_comment"
            
        # Nếu không visible, check DOM rồi thử reveal
        if form is None:
            if await _exists_in_dom(combined_cand):
                try:
                    await combined_cand.first.evaluate("el => { el.style.display = ''; el.style.visibility = 'visible'; }")
                except Exception:
                    pass
                if await _is_visible(combined_cand):
                    form = combined_cand.first
                    result.strategy = "id/class_comment_revealed"
                    log.debug(f"Layer 2: revealed hidden comment form")

    # ── Layer 3: Heuristic (textarea + email) ────────
    if form is None:
        all_forms_count = await page.locator("form").count()
        for i in range(min(all_forms_count, 10)):  # cap 10 forms tránh treo
            f = page.locator("form").nth(i)
            has_textarea = await f.locator("textarea").count() > 0
            has_email = (
                await f.locator("input[type='email']").count() > 0
                or await f.locator("input[name*='email' i]").count() > 0
            )
            if has_textarea and has_email:
                form = f
                result.strategy = "heuristic"
                break

    # ── Layer 4: Fallback (form cuối cùng có textarea) ─
    if form is None:
        all_forms_count = await page.locator("form").count()
        for i in range(min(all_forms_count, 10) - 1, -1, -1):
            f = page.locator("form").nth(i)
            if await f.locator("textarea").count() > 0:
                form = f
                result.strategy = "fallback_last_textarea"
                break

    # ── Layer 5: Textarea nằm ngoài form (AJAX comment) ──
    if form is None:
        container_sels = [
            "#respond",
            ".comment-respond",
            ".comments-area",
            "[id*='comment-form']",
            "[class*='comment-form']",
            "#review_form_wrapper",
            ".review-form",
        ]
        combined_sel = ", ".join(container_sels)
        c_loc = page.locator(combined_sel)
        if await _is_visible(c_loc):
            if await c_loc.first.locator("textarea").count() > 0:
                form = c_loc.first
                result.strategy = "container_no_form"
                log.debug(f"Layer 5: found textarea in container {combined_sel}")

    # ── Layer 6: Tìm comment form trong iframe ──
    if form is None:
        for frame in page.frames[1:4]:  # max 3 iframes
            try:
                if frame.is_detached():
                    continue
                iframe_form = frame.locator("form")
                cnt = await iframe_form.count()
                for i in range(min(cnt, 5)):
                    f = iframe_form.nth(i)
                    if await f.locator("textarea").count() > 0:
                        form = f
                        result.strategy = "iframe_form"
                        log.debug("Layer 6: found form inside iframe")
                        break
                if form is not None:
                    break
            except Exception:
                pass

    if form is None:
        log.debug("find_comment_form: no form found after all layers")
        return result  # found=False

    result.found = True
    result.form = form
    log.debug(f"find_comment_form: found via strategy={result.strategy}")

    fields = await _extract_fields(page, form)
    result.f_name    = fields["name"]
    result.f_email   = fields["email"]
    result.f_url     = fields["url"]
    result.f_comment = fields["comment"]
    result.f_submit  = fields["submit"]

    # Rating detection
    rating_loc, rating_type = await _find_rating(page, form)
    result.f_rating = rating_loc
    result.rating_type = rating_type
    if rating_type != "none":
        log.debug(f"Rating detected: type={rating_type}")

    result.has_captcha = await _has_captcha(page)

    return result
