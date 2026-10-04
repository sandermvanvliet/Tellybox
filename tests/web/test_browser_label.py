"""Browser label from the User-Agent (PB-7, WT-12): short, server-side, with a fallback."""

import pytest

from tellybox.web.browser_label import LABEL_MAX, browser_label

IPHONE_SAFARI = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                 "Version/17.4 Mobile/15E148 Safari/604.1")
IPHONE_CHROME = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                 "CriOS/124.0.6367.88 Mobile/15E148 Safari/604.1")
ANDROID_CHROME = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Mobile Safari/537.36")
ANDROID_SAMSUNG = ("Mozilla/5.0 (Linux; Android 14; SM-S911B) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "SamsungBrowser/24.0 Chrome/117.0.0.0 Mobile Safari/537.36")
WINDOWS_FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0"
WINDOWS_EDGE = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0")
MAC_SAFARI = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
              "Version/17.4 Safari/605.1.15")


@pytest.mark.parametrize("ua, label", [
    (IPHONE_SAFARI, "iPhone Safari"),
    (IPHONE_CHROME, "iPhone Chrome"),
    (ANDROID_CHROME, "Android Chrome"),
    (ANDROID_SAMSUNG, "Android Samsung Internet"),
    (WINDOWS_FIREFOX, "Windows Firefox"),
    (WINDOWS_EDGE, "Windows Edge"),
    (MAC_SAFARI, "Mac Safari"),
])
def test_label(ua, label):
    assert browser_label(ua) == label


@pytest.mark.parametrize("ua", [None, "", "curl/8.5.0", "x" * 500])
def test_fallback(ua):
    assert browser_label(ua) == "Browser"


def test_never_longer_than_the_limit():
    assert len(browser_label("Android " + "Firefox/1 " * 100)) <= LABEL_MAX
