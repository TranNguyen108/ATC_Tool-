"""
human_sim.py — Mô phỏng hành vi người dùng thật để tránh bot detection.
Tất cả hàm đều là async, dùng với Playwright.
"""

import asyncio
import random


async def random_pause(min_s: float = 0.5, max_s: float = 2.0) -> None:
    """Sleep ngẫu nhiên trong khoảng [min_s, max_s] giây."""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def mouse_approach(page, locator) -> None:
    """Di chuyển chuột đến gần element + dispatch mouseenter/mouseover.
    Thêm ~50-80ms, giúp Akismet thấy có mouse activity."""
    try:
        box = await locator.bounding_box(timeout=1500)
        if not box:
            return
        # Target: center of element with small random offset
        tx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        ty = box["y"] + box["height"] * random.uniform(0.3, 0.7)
        await page.mouse.move(tx, ty, steps=random.randint(2, 4))
        await locator.dispatch_event("mouseenter", timeout=500)
        await locator.dispatch_event("mouseover", timeout=500)
    except Exception:
        pass


async def emit_form_signals(page, form_locator) -> None:
    """Dispatch focusin + mouseover on form container — mimic real user focus.
    Called once after form detection, adds ~50-100ms."""
    try:
        await form_locator.dispatch_event("mouseover", timeout=500)
        await form_locator.dispatch_event("focusin", timeout=500)
    except Exception:
        pass
    await asyncio.sleep(random.uniform(0.05, 0.1))


async def fast_fill(locator, text: str, page=None) -> None:
    """
    Điền text instant qua fill() — dùng cho name/email/url (không cần slow-type).
    Mỗi Playwright call có timeout riêng để fail SẠCH nếu element bị vấn đề.
    Không dùng asyncio.wait_for bên ngoài — tránh cancel coroutine → orphaned future.
    """
    if page:
        await mouse_approach(page, locator)
    try:
        await locator.evaluate("el => el.scrollIntoView({behavior: 'instant', block: 'center'})")
    except Exception:
        pass

    try:
        await locator.click(timeout=1500, force=True)
    except Exception:
        try:
            await locator.evaluate("el => el.focus()")
        except Exception:
            pass
    await asyncio.sleep(random.uniform(0.05, 0.12))
    try:
        await locator.fill(text, timeout=3000, force=True)
    except Exception:
        return  # field không fillable — bỏ qua
    try:
        await locator.dispatch_event("input", timeout=1000)
        await locator.dispatch_event("change", timeout=1000)
    except Exception:
        pass
    await asyncio.sleep(random.uniform(0.08, 0.18))


async def type_text(locator, text: str, page=None) -> None:
    """
    Điền comment theo chunks (3-5 đoạn).

    Per-word fill cũ: 40 từ × (fill + dispatch + sleep) = ~40 Playwright calls = 3-5s
      → Quá nhiều call = quá nhiều cơ hội treo nếu element detach

    Chunk fill mới: chia text thành 3-5 phần → 3-5 call fill() = ~1-2s total
      → Ít call = ít treo, vẫn không instant (tránh "nhập quá nhanh")
    """
    if page:
        await mouse_approach(page, locator)
    try:
        await locator.evaluate("el => el.scrollIntoView({behavior: 'instant', block: 'center'})")
    except Exception:
        pass

    try:
        await locator.click(timeout=1500, force=True)
    except Exception:
        try:
            await locator.evaluate("el => el.focus()")
        except Exception:
            pass
    await asyncio.sleep(random.uniform(0.1, 0.2))

    words = text.split()
    if not words:
        try:
            await locator.fill(text, timeout=2000, force=True)
        except Exception:
            try:
                await locator.evaluate("el => { el.value = arguments[0]; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); }", text)
            except Exception:
                pass
        return

    n_chunks = min(len(words), random.randint(3, 5))
    chunk_size = max(1, len(words) // n_chunks)
    accumulated = ""

    success = True
    for i in range(0, len(words), chunk_size):
        chunk = words[i:i + chunk_size]
        accumulated += (" " if accumulated else "") + " ".join(chunk)
        try:
            await locator.fill(accumulated, timeout=2500, force=True)
        except Exception:
            success = False
            break
        await asyncio.sleep(random.uniform(0.2, 0.4))

    if not success:
        # Neu fill bi block boi overlay/timeout, fallback sang dien JS
        try:
            await locator.evaluate(
                "(el, v) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); }",
                text
            )
        except Exception:
            pass

    try:
        await locator.dispatch_event("input", timeout=500)
        await locator.dispatch_event("change", timeout=500)
    except Exception:
        pass
    await asyncio.sleep(random.uniform(0.1, 0.2))


async def jump_to_bottom(page) -> None:
    """
    Nhảy thẳng xuống cuối trang ngay lập tức bằng JS (behavior: instant).
    Dùng TRƯỚC khi tìm comment form — form thường nằm ở cuối trang.
    Sleep giảm xuống 0.15–0.35s (đủ để DOM ổn định, không gây timeout).
    Retry scroll sẽ được xử lý ở worker nếu form chưa tìm thấy.
    """
    await page.evaluate("window.scrollTo({top: document.body.scrollHeight, behavior: 'instant'})")
    await asyncio.sleep(random.uniform(0.15, 0.35))


async def human_scroll(page, target_y: int | None = None) -> None:
    """
    Scroll dần dần đến target_y (pixel). Nếu target_y=None thì scroll xuống cuối trang.
    - Mỗi step: 80–200px
    - Delay giữa các step: 30–80ms
    - Thỉnh thoảng có micro-pause dài hơn (mô phỏng đọc nội dung)
    Dùng khi cần mô phỏng hành vi đọc, KHÔNG dùng trước khi tìm form
    (dùng jump_to_bottom() thay thế để tránh timeout).
    """
    if target_y is None:
        target_y = await page.evaluate("() => document.body.scrollHeight")

    current_y: int = await page.evaluate("() => window.scrollY")

    step_count = 0
    while current_y < target_y - 50:
        step = random.randint(80, 200)
        current_y = min(current_y + step, target_y)
        await page.evaluate(f"window.scrollTo({{top: {current_y}, behavior: 'instant'}})")
        await asyncio.sleep(random.uniform(0.03, 0.08))

        step_count += 1
        # Dừng lại thỉnh thoảng như đang đọc nội dung
        if step_count % random.randint(5, 10) == 0:
            await asyncio.sleep(random.uniform(0.3, 0.8))


async def scroll_element_into_view(page, locator) -> None:
    """Scroll để element vào giữa viewport."""
    try:
        await locator.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass  # không scroll được — vẫn thử fill
    await asyncio.sleep(random.uniform(0.1, 0.25))


async def human_click(locator, no_wait_after: bool = False) -> None:
    """Click với delay nhỏ trước và sau.

    Args:
        no_wait_after: True cho submit button — không chờ navigation xong.
            Mặc định Playwright click() chờ cho "scheduled navigations to finish".
            Khi click submit → page navigate → chờ navigation = chờ page load mới.
            Nếu page load > timeout → TimeoutError → code tưởng form thất bại
            (thực tế form đã submit thành công, chỉ response chưa xong).
    """
    await asyncio.sleep(random.uniform(0.05, 0.15))
    # timeout=8000: đủ dài cho page xử lý overlay/animation trước khi click thực sự
    await locator.click(timeout=8000, no_wait_after=no_wait_after)
    await asyncio.sleep(random.uniform(0.05, 0.2))
