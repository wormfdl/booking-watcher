# -*- coding: utf-8 -*-
"""
대추밭백한의원 취소표 감시기 - 경량 API 버전 (v3)
- Playwright/Chrome 불필요 (파이썬 표준 라이브러리만 사용)

v3에서 바꾼 것 (중요):
  이전 버전은 "시간대별 정원/예약수"를 직접 비교해서 빈자리를 판단했는데,
  이 값이 실제 "예약 탭 노출 여부"와 일치하지 않는다는 게 확인되었습니다.

  네이버 예약 페이지에 있는 진짜 마스터 스위치는 bizItems 쿼리의
  `bookingAvailableValue` 값으로 보입니다. 이 값이 0일 때 실제로
  네이버 지도의 "예약" 탭이 사라진 상태였고, 이 값이 바뀌면 예약 탭이
  뜨는 것으로 추정됩니다 (병원 공지: "취소표가 있으면 실시간으로
  예약창이 오픈됩니다"와 정확히 일치).

  그래서 이제부터는 이 값(그리고 isClosedBooking, bookableSettingJson)의
  '변화'만 감시하고, 변화가 감지되면 시간대별 상세 정보는 참고용으로만
  같이 보여줍니다.
"""
import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timedelta

BUSINESS_ID = "1359557"
BIZ_ITEM_ID = "6566444"
BUSINESS_TYPE_ID = 13
PLACE_ID = "13258169"

MONITOR_DAYS = 100  # 참고용 시간대 상세 조회 범위

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

GRAPHQL_URL = "https://m.booking.naver.com/graphql"

BIZITEMS_QUERY = """query bizItems($input: BizItemsParams) {
  bizItems(input: $input) {
    id
    bizItemId
    isClosedBooking
    isClosedBookingUser
    bookingAvailableCode
    bookingAvailableValue
    bookableSettingJson
    __typename
  }
}"""

SCHEDULE_QUERY = """query hourlySchedule($scheduleParams: ScheduleParams) {
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


def call_graphql(query, variables, op_name):
    body = {"operationName": op_name, "variables": variables, "query": query}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{GRAPHQL_URL}?opName={op_name}",
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
    return parsed["data"]


def fetch_master_status():
    data = call_graphql(
        BIZITEMS_QUERY,
        {"input": {"businessId": BUSINESS_ID, "lang": "ko", "projections": "RESOURCE"}},
        "bizItems",
    )
    items = data.get("bizItems") or []
    if not items:
        raise RuntimeError("bizItems 응답이 비어있습니다")
    item = items[0]
    return {
        "isClosedBooking": item.get("isClosedBooking"),
        "isClosedBookingUser": item.get("isClosedBookingUser"),
        "bookingAvailableCode": item.get("bookingAvailableCode"),
        "bookingAvailableValue": item.get("bookingAvailableValue"),
        "isPaused": (item.get("bookableSettingJson") or {}).get("isPaused"),
        "isOpened": (item.get("bookableSettingJson") or {}).get("isOpened"),
    }


def fetch_detail_hint(now: datetime):
    """참고용: 지금 시간대별로 정원보다 예약이 적은 곳이 있는지 (확정 정보는 아님)"""
    today = datetime.now().date()
    start = today.strftime("%Y-%m-%d")
    end = (today + timedelta(days=MONITOR_DAYS)).strftime("%Y-%m-%d")
    data = call_graphql(
        SCHEDULE_QUERY,
        {
            "scheduleParams": {
                "businessTypeId": BUSINESS_TYPE_ID,
                "businessId": BUSINESS_ID,
                "bizItemId": BIZ_ITEM_ID,
                "startDateTime": f"{start}T00:00:00",
                "endDateTime": f"{end}T23:59:59",
                "fixedTime": True,
                "includesHolidaySchedules": True,
            }
        },
        "hourlySchedule",
    )
    hourly = data["schedule"]["bizItemSchedule"]["hourly"]
    hints = []
    for h in hourly:
        if not h.get("isUnitBusinessDay") or not h.get("isUnitSaleDay"):
            continue
        stock, booked = h.get("unitStock"), h.get("unitBookingCount")
        if stock is None or booked is None or booked >= stock:
            continue
        start_dt_str = h.get("unitStartDateTime")
        if start_dt_str:
            try:
                if datetime.strptime(start_dt_str, "%Y-%m-%dT%H:%M:%SZ") <= now:
                    continue
            except ValueError:
                pass
        hints.append(f"{h['unitStartTime']} (정원 {stock}/예약 {booked})")
    return hints


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


def load_previous_status():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 이전 버전(v1/v2) 형식이면 호환되지 않으므로 첫 실행으로 취급
        if not isinstance(data, dict) or "bookingAvailableValue" not in data:
            return None
        return data
    except Exception:
        return None


def save_status(status):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


def main():
    now = datetime.utcnow()
    status = fetch_master_status()
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 현재 상태: {status}")

    previous = load_previous_status()

    if previous is None:
        print("첫 실행입니다. 현재 상태를 기준선으로만 저장하고, 알림은 보내지 않습니다.")
        save_status(status)
        return

    changed = (
        previous.get("bookingAvailableValue") != status.get("bookingAvailableValue")
        or previous.get("bookingAvailableCode") != status.get("bookingAvailableCode")
        or previous.get("isClosedBooking") != status.get("isClosedBooking")
        or previous.get("isPaused") != status.get("isPaused")
    )

    if changed:
        print("변화 감지! 이전:", previous, "/ 지금:", status)
        try:
            hints = fetch_detail_hint(now)
        except Exception as e:
            hints = []
            print(f"상세 정보 조회 실패(무시): {e}")

        lines = [
            "🦷 대추밭백한의원 예약 상태 변화 감지!",
            "",
            f"이전: bookingAvailableValue={previous.get('bookingAvailableValue')}, code={previous.get('bookingAvailableCode')}",
            f"지금: bookingAvailableValue={status.get('bookingAvailableValue')}, code={status.get('bookingAvailableCode')}",
            "",
            "네이버 지도에서 '예약' 탭이 떴는지 직접 확인하세요:",
            f"https://pcmap.place.naver.com/hospital/{PLACE_ID}/home",
        ]
        if hints:
            lines.append("")
            lines.append("참고로 정원보다 예약이 적은 시간대(확정 정보 아님):")
            for h in hints[:15]:
                lines.append(f"- {h}")

        send_telegram("\n".join(lines))
    else:
        print("변화 없음 (알림 안 보냄)")

    save_status(status)


if __name__ == "__main__":
    main()
