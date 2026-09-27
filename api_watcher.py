# -*- coding: utf-8 -*-
"""
대추밭백한의원 취소표 감시기 - 경량 API 버전 (v4)
- Playwright/Chrome 불필요 (파이썬 표준 라이브러리만 사용)

v4에서 바꾼 것 (중요):
  v3는 bizItems.bookingAvailableValue를 "마스터 스위치"로 보고 그 값이
  바뀔 때만 알림을 보냈습니다. 그런데 실제로 확인해보니 이 값(그리고
  business 쿼리의 bookingAvailableCode/Value 도 마찬가지)은 예약 탭이
  사라졌다 다시 나타나는 동안에도 전혀 바뀌지 않았습니다. 즉 이 필드들은
  실제 "취소표 발생"과 무관한 값이었습니다.

  그래서 v4는 더 이상 이런 "상태값"에 의존하지 않습니다. 대신 시간대별
  (hourlySchedule) 정원(unitStock)-예약(unitBookingCount) 값을 5분마다
  스냅샷으로 저장해두고, 바로 다음 스냅샷과 비교해서 "특정 시간대의
  빈자리 수가 이전보다 늘어났는지"만 봅니다.

  - 빈자리가 늘었다 = 누군가 취소했거나(취소표 발생), 새 날짜가
    예약 오픈됐다(예: 12월 예약 10/25 13시 오픈) 는 뜻이므로 100% 실제
    변화입니다. "마감"이라는 라벨이 무엇을 의미하는지 몰라도 상관없이,
    숫자가 늘어나는 순간 자체는 거짓일 수 없습니다.
  - 반대로 원래도 비어있던 자리가 계속 비어있는 건(=이전과 값이 같음)
    알림을 보내지 않습니다. → v1에서 있었던 "이미 있던 자리를 전부
    새 자리로 착각해서 우르르 알림 보내는" 문제가 구조적으로 없습니다.
  - 첫 실행(state.json 없음)은 항상 기준선만 저장하고 알림 없음.
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

MONITOR_DAYS = 100  # 오늘부터 몇일치 시간대를 감시할지 (12월 오픈까지 커버)

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

GRAPHQL_URL = "https://m.booking.naver.com/graphql"

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


def fetch_slots(now: datetime):
    """시간대별 (정원 - 예약) 스냅샷을 딕셔너리로 반환: {slot_key: 빈자리수}"""
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

    slots = {}
    meta = {}
    for h in hourly:
        if not h.get("isUnitBusinessDay") or not h.get("isUnitSaleDay"):
            continue
        start_dt_str = h.get("unitStartDateTime")
        if start_dt_str:
            try:
                if datetime.strptime(start_dt_str, "%Y-%m-%dT%H:%M:%SZ") <= now:
                    continue
            except ValueError:
                pass
        stock, booked = h.get("unitStock"), h.get("unitBookingCount")
        if stock is None or booked is None:
            continue
        key = f"{h.get('id')}|{start_dt_str}"
        available = max(0, stock - booked)
        slots[key] = available
        meta[key] = {
            "date": start_dt_str,
            "time": h.get("unitStartTime"),
            "stock": stock,
            "booked": booked,
        }
    return slots, meta


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


def load_previous_slots():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # v1/v2/v3 형식이면 호환되지 않으므로 첫 실행으로 취급
        if not isinstance(data, dict) or "slots" not in data or not isinstance(data["slots"], dict):
            return None
        return data["slots"]
    except Exception:
        return None


def save_slots(slots):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"slots": slots, "updated": datetime.now().isoformat()}, f, ensure_ascii=False, indent=2)


def main():
    now = datetime.utcnow()
    slots, meta = fetch_slots(now)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 감시 대상 시간대 {len(slots)}개 조회 완료")

    previous = load_previous_slots()

    if previous is None:
        print("첫 실행(또는 이전 버전 상태파일)입니다. 현재 상태를 기준선으로만 저장하고, 알림은 보내지 않습니다.")
        save_slots(slots)
        return

    increased = []
    for key, available in slots.items():
        prev_available = previous.get(key, 0)
        if available > prev_available:
            increased.append((key, prev_available, available))

    if increased:
        print(f"빈자리 증가 감지! {len(increased)}건")
        increased.sort(key=lambda x: meta[x[0]]["date"] or "")
        lines = [
            "🦷 대추밭백한의원 취소표(빈자리) 발생!",
            "",
        ]
        for key, prev_a, now_a in increased[:15]:
            m = meta[key]
            date_str = (m["date"] or "").replace("T", " ").replace("Z", "")
            lines.append(
                f"- {date_str} {m['time']}  (정원 {m['stock']} / 예약 {m['booked']}, 빈자리 {prev_a}→{now_a})"
            )
        if len(increased) > 15:
            lines.append(f"...외 {len(increased) - 15}건 더")
        lines.append("")
        lines.append("아래 링크에서 바로 예약을 확인/진행하세요:")
        lines.append(f"https://m.booking.naver.com/booking/13/bizes/{BUSINESS_ID}/items/{BIZ_ITEM_ID}")
        send_telegram("\n".join(lines))
    else:
        print("변화 없음 (알림 안 보냄)")

    save_slots(slots)


if __name__ == "__main__":
    main()
