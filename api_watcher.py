# -*- coding: utf-8 -*-
"""
대추밭백한의원 취소표 감시기 - 경량 API 버전 (v6)

v6에서 바꾼 것:
  v5는 "예약 탭이 진짜로 뜬 것"이 확인될 때만 알림을 보냈는데,
  그 확인 신호(플레이스 페이지의 naverBooking 값)가 뜨기 *전에*
  이미 시간대별 빈자리 숫자(hourlySchedule)가 먼저 바뀌는 경우가 있을 수
  있습니다. 5분 주기 안에서는 1초라도 더 빨리 아는 게 유리할 수 있으므로,
  두 신호를 다 알림으로 보내되 성격을 구분합니다.

  - ⚡ 조기 신호(미확인): 시간대별 빈자리 수가 이전보다 늘어남을 감지.
    과거에 실제로 틀렸던 적이 있는 신호라서 "아직 확정 아님"으로 표시합니다.
  - ✅ 확인됨: 네이버 지도 플레이스 페이지의 naverBooking.bookingBusinessId /
    naverBookingUrl 이 null → 값 있음으로 바뀌는 것을 확인. 이건 "예약" 탭이
    실제로 화면에 뜨는 원본 데이터라서 신뢰도가 가장 높습니다.

  같은 회차에 두 조건이 동시에 만족되면(탭도 열리고 숫자도 늘어난 경우)
  혼란을 줄이기 위해 확인됨(✅) 알림만 보냅니다.
"""
import json
import os
import re
import urllib.request
import urllib.error
from datetime import datetime, timedelta

BUSINESS_ID = "1359557"
BIZ_ITEM_ID = "6566444"
BUSINESS_TYPE_ID = 13
PLACE_ID = "13258169"

MONITOR_DAYS = 100  # 시간대별 조회 범위

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

GRAPHQL_URL = "https://m.booking.naver.com/graphql"
PLACE_URL = f"https://pcmap.place.naver.com/hospital/{PLACE_ID}/home"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)

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
            "User-Agent": UA,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    parsed = json.loads(raw)
    if "errors" in parsed:
        raise RuntimeError(f"GraphQL 에러: {parsed['errors']}")
    return parsed["data"]


def fetch_tab_status():
    """네이버 지도 플레이스 홈 페이지 원본 데이터에서, '예약' 탭을 실제로
    노출시키는 naverBooking 객체를 직접 읽어온다. (진짜 탭 유무 신호)"""
    req = urllib.request.Request(
        PLACE_URL,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    m = re.search(r'"naverBooking":\{.*?\}', html)
    if not m:
        raise RuntimeError(
            "페이지에서 naverBooking 데이터를 찾지 못했습니다 "
            "(네이버가 페이지 구조를 바꿨거나, 요청이 차단됐을 수 있습니다)"
        )
    blob = m.group(0)

    def extract(key):
        mm = re.search(rf'"{key}":("(?:[^"\\]|\\.)*"|null|true|false|-?\d+(?:\.\d+)?)', blob)
        return None if not mm else mm.group(1)

    booking_business_id = extract("bookingBusinessId")
    naver_booking_url = extract("naverBookingUrl")

    def is_present(v):
        return v is not None and v != "null"

    return {
        "bookingBusinessId": booking_business_id,
        "naverBookingUrl": naver_booking_url,
        "tab_open": is_present(booking_business_id) or is_present(naver_booking_url),
    }


def fetch_slots(now: datetime):
    """시간대별 (정원 - 예약) 스냅샷. {slot_key: 빈자리수}, {slot_key: 상세정보}"""
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
            "date": (start_dt_str or "").replace("T", " ").replace("Z", ""),
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


def load_previous_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "tab_open" not in data or "slots" not in data:
            return None
        return data
    except Exception:
        return None


def save_state(tab_open, slots):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"tab_open": tab_open, "slots": slots, "updated": datetime.now().isoformat()},
            f,
            ensure_ascii=False,
            indent=2,
        )


def main():
    now = datetime.utcnow()

    try:
        tab_status = fetch_tab_status()
        tab_open_now = tab_status["tab_open"]
    except Exception as e:
        print(f"[탭 상태 확인 실패] {e}")
        tab_status = None
        tab_open_now = None

    try:
        slots_now, meta = fetch_slots(now)
    except Exception as e:
        print(f"[시간대 조회 실패] {e}")
        slots_now, meta = None, None

    if tab_open_now is None and slots_now is None:
        print("이번 회차는 두 조회가 모두 실패해서 건너뜁니다.")
        return

    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] tab_open={tab_open_now}, "
          f"조회된 시간대 수={len(slots_now) if slots_now is not None else 'N/A'}")

    previous = load_previous_state()

    if previous is None:
        print("첫 실행(또는 이전 버전 상태파일)입니다. 현재 상태를 기준선으로만 저장하고, 알림은 보내지 않습니다.")
        save_state(
            tab_open_now if tab_open_now is not None else False,
            slots_now if slots_now is not None else {},
        )
        return

    prev_tab_open = previous.get("tab_open", False)
    prev_slots = previous.get("slots", {})

    confirmed = (tab_open_now is True) and (prev_tab_open is False)

    increased = []
    if slots_now is not None:
        for key, available in slots_now.items():
            prev_available = prev_slots.get(key, 0)
            if available > prev_available:
                increased.append((key, prev_available, available))
        increased.sort(key=lambda x: meta[x[0]]["date"] or "")

    if confirmed:
        print("✅ 확인됨: 예약 탭 오픈 감지!")
        lines = [
            "✅ [확인됨] 대추밭백한의원 예약 탭이 열렸습니다!",
            "",
            "바로 확인해보세요:",
            f"https://pcmap.place.naver.com/hospital/{PLACE_ID}/home",
        ]
        if increased:
            lines.append("")
            lines.append("참고: 빈자리가 늘어난 시간대")
            for key, prev_a, now_a in increased[:15]:
                m = meta[key]
                lines.append(f"- {m['date']} {m['time']} (정원 {m['stock']}/예약 {m['booked']}, 빈자리 {prev_a}→{now_a})")
        send_telegram("\n".join(lines))

    elif increased and not (tab_open_now is True):
        print(f"⚡ 조기 신호(미확인): {len(increased)}건")
        lines = [
            "⚡ [조기 신호 - 아직 미확인] 대추밭백한의원 빈자리 수 변화 감지",
            "(주의: 이 신호는 예전에 실제로는 예약이 안 열려있던 적도 있었습니다. 참고만 하세요)",
            "",
        ]
        for key, prev_a, now_a in increased[:15]:
            m = meta[key]
            lines.append(f"- {m['date']} {m['time']} (정원 {m['stock']}/예약 {m['booked']}, 빈자리 {prev_a}→{now_a})")
        lines.append("")
        lines.append(f"https://m.booking.naver.com/booking/13/bizes/{BUSINESS_ID}/items/{BIZ_ITEM_ID}")
        send_telegram("\n".join(lines))

    else:
        print("변화 없음 (알림 안 보냄)")

    save_state(
        tab_open_now if tab_open_now is not None else prev_tab_open,
        slots_now if slots_now is not None else prev_slots,
    )


if __name__ == "__main__":
    main()
