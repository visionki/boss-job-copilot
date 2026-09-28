from __future__ import annotations
import json
from pathlib import Path
from urllib.parse import urlsplit
from .local import Stopped


# Shared by page health checks and each outreach DOM operation. Inspect visible
# feedback, excluding job prose, message history and editable drafts.
CONTACT_LIMIT = r"""(() => {
    if (!document.body || !['www.zhipin.com','zhipin.com'].includes(location.hostname)) return null;
    const ignored = '.job-sec-text,.job-detail-description,.company-info-box,.message-list,' +
        '.item-myself,.message-item,#chat-input,textarea,[contenteditable],script,style';
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT), texts = [];
    let node;
    while ((node = walker.nextNode())) {
        const e = node.parentElement;
        if (!e || e.closest(ignored) || !e.getClientRects().length ||
            getComputedStyle(e).visibility !== 'visible') continue;
        const text = node.textContent.trim();
        if (text) texts.push(text);
    }
    const compact = texts.join('').replace(/\s+/g, '');
    const title = compact.match(/(?:今日|今天|当日)(?:沟通|打招呼)(?:次数|机会|额度)?(?:已|已经)?(?:达(?:到)?上限|用完)|(?:您|你)?已(?:达到|达)沟通上限|沟通次数已(?:达到|达)上限/);
    if (!title) return null;
    const usage = compact.match(/(?:您|你)?(?:今天|今日|当日)已与(\d+)位BOSS沟通/i);
    const retry = compact.match(/(?:明天|明日)再(?:来|试|沟通)/);
    const daily = !!(usage || retry || /今日|今天|当日/.test(title[0]));
    return {reason: daily ? 'platform_contact_limit_daily' : 'platform_contact_limit', daily,
        text: [title[0], usage?.[0], retry?.[0]].filter(Boolean).join('；'),
        reported_contacts: usage ? Number(usage[1]) : null, retry_hint: retry?.[0] || null};
})"""


class ContactLimitReached(Stopped):
    def __init__(self, limit):
        self.limit = limit
        super().__init__(limit['reason'])


def page_error(page, reason):
    limit = page.get('contact_limit')
    return ContactLimitReached(limit) if limit and reason == limit['reason'] else Stopped(reason)


def contact_limit_feedback(reason, limit=None):
    if 'platform_contact_limit' not in reason:
        return {}
    daily = reason.endswith('platform_contact_limit_daily')
    return {'contact_limit': limit or {'reason': reason.split(':')[-1], 'daily': daily},
            'user_message': ('BOSS 提示今日沟通次数已达上限，已停止网页操作。请按平台提示明天再试，再显式检查页面。'
                             if daily else 'BOSS 提示沟通次数已达上限，已停止网页操作。请按平台提示处理后再显式检查页面。'),
            'next_action': 'wait_until_tomorrow_then_check' if daily else 'handle_platform_limit_then_check'}


def separate_company_text(detail: dict) -> dict:
    """Some pages put the company section inside the broad JD selector too."""
    result = dict(detail)
    jd = result.get("job_detail")
    company = result.get("company_info")
    if isinstance(jd, str) and isinstance(company, str) and company.strip():
        if jd.rstrip().endswith(company.strip()):
            result["job_detail"] = jd.rstrip()[:-len(company.strip())].rstrip()
    return result


READ_PAGE = r"""(() => {
    const visible = e => e && e.getClientRects().length;
    const text = selector => Array.from(document.querySelectorAll(selector))
        .filter(visible).map(e => e.innerText.trim()).filter(Boolean).join('\n');
    // Online and last-active labels are rendered as different elements in the recruiter card.
    const activityLabels = Array.from(document.querySelectorAll(
        '.job-boss-info .boss-online-tag, .job-boss-info .boss-active-time'))
        .filter(visible).map(e => e.innerText.trim()).filter(Boolean);
    return JSON.stringify({
        url: location.origin === 'null' ? location.href.split(/[?#]/)[0] : location.origin + location.pathname,
        title: document.title, document_id: performance.timeOrigin,
        text: document.body ? document.body.innerText : '',
        contact_limit: __CONTACT_LIMIT__(),
        logged_in: Array.from(document.querySelectorAll(
            '.user-nav .nav-figure, .nav-figure, .user-nav a[href*="chat"]')).some(visible),
        job_detail: text('.job-detail-section .job-sec-text') || text('.job-detail-description'),
        company_info: text('.company-info-box .job-sec-text'),
        work_address: text('.location-address'),
        chat_button: text('.btn-startchat'),
        recruiter_active_label: activityLabels.includes('在线') ? '在线' : activityLabels[0] || null
    });
})()""".replace('__CONTACT_LIMIT__', CONTACT_LIMIT)

CHALLENGE = ("访问过于频繁", "访问行为异常", "请完成安全验证", "请拖动滑块",
             "请完成下方验证", "异常访问", "账号存在异常", "请进行安全验证")
TRUNCATED = ("登录后查看", "登录查看更多", "登录后可查看", "登录查看完整", "展开全部", "展开更多")


def page_problem(page: dict, require_login: bool = True) -> str | None:
    text, url = page.get("text", ""), page.get("url", "")
    if any(word in text for word in CHALLENGE) or any(part in url for part in ("/captcha", "/safe/", "/verify")):
        return "verification_required"
    if urlsplit(url).hostname not in ("www.zhipin.com", "zhipin.com"):
        return "blank_or_redirected"
    if page.get('contact_limit'):
        return page['contact_limit']['reason']
    if require_login and not page.get("logged_in") and any(word in text for word in ("扫码登录", "短信登录", "登录/注册", "登录注册", *TRUNCATED)):
        return "login_required"
    if len(text.strip()) < 40:
        return "blank_or_redirected"
    if any(word in text for word in ("该职位已关闭", "该职位已下线", "职位不存在", "职位已失效")):
        return "job_unavailable"
    if require_login and not page.get("logged_in"):
        return "login_required" if any(word in text for word in ("扫码登录", "短信登录", "登录/注册", "登录注册", *TRUNCATED)) else "login_unconfirmed"
    return None


def detail_state(page: dict, job_id: str) -> str:
    page = separate_company_text(page)
    if page_problem(page):
        return "partial"
    if urlsplit(page.get("url", "")).path != f"/job_detail/{job_id}.html":
        return "partial"
    jd = page.get("job_detail", "")
    if len(jd.strip()) < 100 or any(word in page.get("text", "") for word in TRUNCATED):
        return "partial"
    return "complete"


def check_profile_available(profile: Path):
    # A manually opened login window must be closed before starting this owner.
    # Never attach to, stop, or kill a user's existing Chrome process.
    from .browser_process import chrome_processes
    if chrome_processes(profile):
        raise Stopped("profile_in_use_close_existing_boss_window")


def make_driver_config(driver, **kwargs):
    class BrowserConfig(driver.Config):
        def __call__(self):
            # Filter the final argument list: both upstream drivers append some
            # flags in __call__, after _default_browser_args has been assembled.
            retained = []
            removed = []
            for flag in super().__call__():
                if (flag.startswith("--password-store=")
                        or flag.startswith("--remote-allow-origins=")
                        or flag in ("--disable-component-update", "--no-sandbox",
                                    "--disable-web-security", "--disable-site-isolation-trials")
                        or (flag.startswith("--disable-features=")
                            and ("IsolateOrigins" in flag or "site-per-process" in flag))):
                    removed.append(flag)
                else:
                    retained.append(flag)
            self.removed_upstream_flags = removed
            return retained

    return BrowserConfig(**kwargs)
