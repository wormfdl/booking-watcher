# -*- coding: utf-8 -*-
"""
대추밭백한의원 취소표 감시기 - 경량 API 버전
- Playwright/Chrome 불필요 (파이썬 표준 라이브러리만 사용)
- GitHub Actions 등에서 몇 분 간격으로 실행되는 것을 전제로 설계됨
  (한 번 실행 -> 확인 -> 필요하면 텔레그램 알림 -> 종료. 반복 실행은 스케줄러가 담당)
- 이전 실행에서 확인했던 "빈자리 목록"을 state.json에 저장해두고,
  이번 실행에서 "새로 생긴 빈자리"만 골라서 알림을 보냄 (중복 알림 방지)
"""
import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timedelta

# ── 감시 대상 (네이버 예약) ──────────────────────────────────────────
BUSINESS_ID = "1359557"
BIZ_ITEM_ID = "6566444"
BUSINESS_TYPE_ID = 13

# 오늘부터 며칠 뒤까지 감시할지 (100일 = 약 3개월 후까지, 10/11/12월 전부 포함)
MONITOR_DAYS = 100

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

GRAPHQL_URL = "https://m.booking.naver.com/graphql?opName=hourlySchedule"

QUERY = """query hourlySchedule($scheduleParams: ScheduleParams) {
  schedule(input: $scheduleParams) {
    bizItemSchedule {
      hourly {
        id
        unitStartTime
        unitBookingCount
        unitStock
        isUnitBusinessDay
        isUnitSaleDay
        __typename
      }
      __typename
    }
    __typename
  }
}"""

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def fetch_schedule(start_date: str, end_date: str):
    body = {
        "operationName": "hourlySchedule",
        "variables": {
            "scheduleParams": {
                "businessTypeId": BUSINESS_TYPE_ID,
                "businessId": BUSINESS_ID,
                "bizItemId": BIZ_ITEM_ID,
                "startDateTime": f"{start_date}T00:00:00",
                "endDateTime": f"{end_date}T23:59:59",
                "fixedTime": True,
                "includesHolidaySchedules": True,
            }
        },
        "query": QUERY,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    parsed = json.loads(raw)
    if "errors" in parsed:
        raise RuntimeError(f"GraphQL 에러: {parsed['errors']}")
    return parsed["data"]["schedule"]["bizItemSchedule"]["hourly"]


def find_available(hourly):
    """예약 가능(빈자리)한 슬롯만 골라서 {id: 표시용텍스트} 형태로 반환"""
    result = {}
    for h in hourly:
        if not h.get("isUnitBusinessDay"):
            continue
        if not h.get("isUnitSaleDay"):
            continue
        stock = h.get("unitStock")
        booked = h.get("unitBookingCount")
        if stock is None or booked is None:
            continue
        if booked < stock:
            result[h["id"]] = f"{h['unitStartTime']} (정원 {stock} / 예약 {booked})"
    return result


def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[알림 건너뜀] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 설정되지 않았습니다.")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        print("[알림 전송 완료]")
    except urllib.error.URLError as e:
        print(f"[알림 전송 실패] {e}")


def load_previous_ids():
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_current_ids(ids):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False, indent=2)


def main():
    today = datetime.now().date()
    start = today.strftime("%Y-%m-%d")
    end = (today + timedelta(days=MONITOR_DAYS)).strftime("%Y-%m-%d")

    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 확인 범위: {start} ~ {end}")

    hourly = fetch_schedule(start, end)
    available = find_available(hourly)
    current_ids = set(available.keys())

    previous_ids = load_previous_ids()
    new_ids = current_ids - previous_ids

    print(f"현재 빈자리: {len(current_ids)}개 / 이전 대비 새로 생긴 빈자리: {len(new_ids)}개")

    if new_ids:
        lines = ["🦷 대추밭백한의원 취소표 발생!", ""]
        for _id in sorted(new_ids, key=lambda i: available[i]):
            lines.append("- " + available[_id])
        lines.append("")
        lines.append("네이버 예약 페이지에서 바로 확인하세요:")
        lines.append(
            f"https://m.booking.naver.com/booking/13/bizes/{BUSINESS_ID}/items/{BIZ_ITEM_ID}"
        )
        send_telegram("\n".join(lines))
    else:
        print("새로운 빈자리 없음 (알림 안 보냄)")

    save_current_ids(current_ids)


if __name__ == "__main__":
    main()
