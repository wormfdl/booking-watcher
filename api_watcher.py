# -*- coding: utf-8 -*-
"""
대추밭백한의원 취소표 감시기 - 경량 API 버전 (v2)
- Playwright/Chrome 불필요 (파이썬 표준 라이브러리만 사용)
- GitHub Actions 등에서 몇 분 간격으로 실행되는 것을 전제로 설계됨

v2에서 고친 것:
  1. 첫 실행(state.json이 아직 없을 때)에는 "원래 있던 빈자리"까지 전부
     알림으로 보내던 오탐 버그 수정 -> 첫 실행은 조용히 기준선만 저장
  2. 이미 지나간 시간(오늘 중 지난 시각)은 후보에서 제외
  3. 알림 링크에 정확한 날짜(startDate)를 넣어서 클릭하면 바로 그 날짜로 이동
"""
import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timedelta

BUSINESS_ID = "1359557"
BIZ_ITEM_ID = "6566444"
BUSINESS_TYPE_ID = 13
MONITOR_DAYS = 100

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
GRAPHQL_URL = "https://m.booking.naver.com/graphql?opName=hourlySchedule"

QUERY = """query hourlySchedule($scheduleParams: ScheduleParams) {
  schedule(input: $scheduleParams) {
    bizItemSchedule {
      hourly {
        id
        unitStartDateTime
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


def find_available(hourly, now: datetime):
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
        if booked >= stock:
            continue

        start_dt_str = h.get("unitStartDateTime")
        if start_dt_str:
            try:
                start_dt = datetime.strptime(start_dt_str, "%Y-%m-%dT%H:%M:%SZ")
                if start_dt <= now:
                    continue
            except ValueError:
                pass

        local_str = h["unitStartTime"]
        date_part = local_str[:10]
        result[h["id"]] = (f"{local_str} (정원 {stock} / 예약 {booked})", date_part)
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
        return set(), True
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            return set(raw), False
        return set(raw.get("ids", [])), False
    except Exception:
        return set(), True


def save_current_ids(ids):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"ids": sorted(ids)}, f, ensure_ascii=False, indent=2)


def main():
    now = datetime.utcnow()
    today = datetime.now().date()
    start = today.strftime("%Y-%m-%d")
    end = (today + timedelta(days=MONITOR_DAYS)).strftime("%Y-%m-%d")

    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 확인 범위: {start} ~ {end}")

    hourly = fetch_schedule(start, end)
    available = find_available(hourly, now)
    current_ids = set(available.keys())

    previous_ids, is_first_run = load_previous_ids()

    if is_first_run:
        print(f"첫 실행입니다. 현재 빈자리 {len(current_ids)}개를 기준선으로만 저장하고, 알림은 보내지 않습니다.")
        save_current_ids(current_ids)
        return

    new_ids = current_ids - previous_ids
    print(f"현재 빈자리: {len(current_ids)}개 / 이전 대비 새로 생긴 빈자리: {len(new_ids)}개")

    if new_ids:
        lines = ["🦷 대추밭백한의원 취소표 발생!", ""]
        for _id in sorted(new_ids, key=lambda i: available[i][0]):
            text, date_part = available[_id]
            lines.append(
                f"- {text}\n  https://m.booking.naver.com/booking/13/bizes/{BUSINESS_ID}/items/{BIZ_ITEM_ID}?startDate={date_part}"
            )
        send_telegram("\n".join(lines))
    else:
        print("새로운 빈자리 없음 (알림 안 보냄)")

    save_current_ids(current_ids)


if __name__ == "__main__":
    main()
