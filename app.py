import os
import streamlit as st
import pandas as pd
import json
import requests
import google.generativeai as genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import re
import uuid

st.set_page_config(page_title="Cloud Budget Manager", layout="wide")

# --- [개선 1] 데이터 무결성을 위한 상수 및 매핑 분리 ---
SHEET_NAME = "Budget_SMS_Receiver"
CALENDAR_DB_ID = "f33998e5-3c5d-83fc-a512-01273de7f5b0"

CATEGORY_PAGE_MAP = {
    "고정지출": "d61998e5-3c5d-8364-8aea-01ef2f470c67",
    "공용 생활비": "e74998e5-3c5d-833a-923d-01d735ee9938",
    "주유비": "fc6998e5-3c5d-82b1-9d65-81f8a42fafdc",
    "종호 지출": "72f998e5-3c5d-8265-8f9e-8124b10989f1",
    "혜송 지출": "651998e5-3c5d-835a-8dc8-81df6de7fe8f",
    "재이 지출": "ef7998e5-3c5d-82a9-a71f-81fbadfae690",
    "여행 문화 쇼핑": "336998e5-3c5d-8378-8318-819c6ec60c76",
    "경조사": "794998e5-3c5d-8230-aec7-810c9cfa4d8f",
    "차관련비용": "304998e5-3c5d-82fd-be44-8196e4270e62",
    "멍게": "363998e5-3c5d-8396-a437-014b0babfd2e",
    "식비": "70b998e5-3c5d-8274-852f-011fe69289a1",
    "의료비": "70e998e5-3c5d-83e4-b57f-011d66ce5771",
    "쇼핑": "d9c998e5-3c5d-83b9-84e6-01ed40d3d673",
    "생필품": "34b998e5-3c5d-834c-9744-81d833885ce1",
    "문화생활, 외출": "8cd998e5-3c5d-8291-9b4e-012fb4c43d62",
    "교육": "81d998e5-3c5d-8215-b2fe-81729990840d"
}

# --- 설정 및 인증 ---
if "authenticated" not in st.session_state:
    if st.query_params.get("pw") == "554477":
        st.session_state.authenticated = True
    else:
        st.session_state.authenticated = False

def check_login():
    st.title("🔒 Budget Manager Login")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")
        if submitted:
            if username == "sing" and password == "554477":
                st.session_state.authenticated = True
                st.query_params.pw = "554477"
                st.rerun()
            else:
                st.error("아이디 또는 비밀번호가 틀렸습니다.")

if not st.session_state.authenticated:
    check_login()
    st.stop()

try:
    gcp_creds = dict(st.secrets["gcp_service_account"])
    GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
    NOTION_TOKEN = st.secrets["NOTION_TOKEN"]
except Exception as e:
    st.error("Streamlit Secrets 설정이 누락되었습니다.")
    st.stop()

genai.configure(api_key=GEMINI_API_KEY)

SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive"
]

@st.cache_resource
def get_gspread_client():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(gcp_creds, SCOPE)
    return gspread.authorize(creds)

client = get_gspread_client()
try:
    sheet = client.open(SHEET_NAME).sheet1
except Exception:
    st.error(f"'{SHEET_NAME}' 구글 시트를 찾을 수 없습니다.")
    st.stop()

# --- 시트 조작 헬퍼 함수 ---
def get_parsed_sheet():
    try:
        return client.open(SHEET_NAME).worksheet("Budget_Parsed")
    except gspread.exceptions.WorksheetNotFound:
        new_sh = client.open(SHEET_NAME).add_worksheet(title="Budget_Parsed", rows="1000", cols="10")
        new_sh.append_row(["date", "merchant", "item_name", "amount", "category", "status", "_row_num", "uid"])
        return new_sh

def get_rules_sheet():
    try:
        return client.open(SHEET_NAME).worksheet("Budget_Rules")
    except gspread.exceptions.WorksheetNotFound:
        new_sh = client.open(SHEET_NAME).add_worksheet(title="Budget_Rules", rows="500", cols="2")
        new_sh.append_row(["merchant", "category"])
        return new_sh

def load_staging_data():
    try:
        return get_parsed_sheet().get_all_records()
    except:
        return []

def save_staging_data_safe(data_list):
    """Ghost Data 문제를 해결한 덮어쓰기 로직"""
    try:
        sh = get_parsed_sheet()
        # 이전 데이터를 지워 Ghost Data가 남지 않도록 보장
        sh.clear()
        
        if not data_list:
            sh.append_row(["date", "merchant", "item_name", "amount", "category", "status", "_row_num", "uid"])
            return
        
        rows = [["date", "merchant", "item_name", "amount", "category", "status", "_row_num", "uid"]]
        for d in data_list:
            rows.append([
                str(d.get("date", "")), str(d.get("merchant", "")), str(d.get("item_name", "")),
                str(d.get("amount", "0")), str(d.get("category", "")), str(d.get("status", "")),
                str(d.get("_row_num", "")), str(d.get("uid", uuid.uuid4().hex))
            ])
        sh.update('A1', rows)
    except Exception as e:
        st.error(f"스테이징 저장 실패: {e}")

def batch_update_sheet_status(row_indices, status_text="Synced"):
    """성공한 행들을 한 번의 API 호출로 모두 업데이트"""
    if not row_indices:
        return
    try:
        requests_list = []
        for row in row_indices:
            requests_list.append({
                'range': f'D{row}',
                'values': [[status_text]]
            })
        sheet.batch_update(requests_list)
    except Exception as e:
        st.error(f"시트 배치 업데이트 실패: {e}")

def load_memory_rules():
    try:
        records = get_rules_sheet().get_all_records()
        return {str(r["merchant"]): str(r["category"]) for r in records if "merchant" in r}
    except:
        return {}

def save_memory_rules(rules):
    try:
        sh = get_rules_sheet()
        sh.clear()
        sh.append_row(["merchant", "category"])
        if rules:
            rows = [[str(k), str(v)] for k, v in rules.items()]
            sh.append_rows(rows)
    except:
        pass

if "parsed_results" not in st.session_state:
    st.session_state.parsed_results = load_staging_data()
if "memory_rules" not in st.session_state:
    st.session_state.memory_rules = load_memory_rules()

# --- [개선 3] LLM 배치 파싱 (비용/성능 최적화) ---
def batch_parse_text(unprocessed_list):
    if not unprocessed_list:
        return []
        
    now = datetime.now()
    
    # 텍스트 합치기 (배치 처리)
    combined_texts = ""
    # 매핑을 위한 딕셔너리 (텍스트 보관용)
    raw_text_map = {}
    for row_num, row in unprocessed_list:
        text = str(row.get("Text", row.get("text", "")))
        combined_texts += f"[ID: {row_num}]\n{text}\n\n"
        raw_text_map[row_num] = text
        
    prompt = f"""
당신은 가계부 데이터 추출 전문가입니다. 현재 연도는 명백히 {now.year}년입니다.
여러 건의 결제 문자가 [ID: 번호] 형태로 주어집니다. 
각 ID별로 분석하여 하나의 JSON 리스트로 응답하세요.

[분류 규칙 및 지시사항]
1. 연도 인식 규칙 (CRITICAL): 입력 텍스트에 연도가 없고 월/일만 있다면 반드시 {now.year}년으로 해석하세요. 절대 과거로 추측하지 마세요!
2. amount: 콤마 제외 숫자만 추출. 단, '취소', '환불', '승인취소' 문구가 있으면 반드시 마이너스(-) 기호를 붙이세요.
3. merchant: 가맹점 명을 추출하되, '네이버페이(배달의민족)'처럼 괄호 정보가 있다면 이를 유지하세요.
4. item_name: 결제 내용에서 품목을 알 수 있다면 5자 내외로 요약하고, 알 수 없으면 빈 문자열로 두세요.
5. category: 제공된 카테고리 목록 내에서 선택. 판단이 모호하면 '알수없음'.
- 고정지출: 관리비, 대출이자, 보험료, 정기구독, 통신비
- 종호 지출: SK세븐모바일, 미니PC, 개인 쇼핑
- 혜송 지출: 아내 관련 지출 (헤송폰요금, 헤송애플 등)
- 재이 지출: 웅진씽크빅, 영어책, 아기용품, 유산균
- 자산 이동: 네이버페이/쿠페이/카카오페이 '출금' (단순 충전)
- 수입: 입금 내역
- 공용 생활비: 식비, 마트 등
- 주유비: 주유소
- 여행 문화 쇼핑: 쇼핑몰, 숙박, 여행
- 경조사: 축의금, 부조금
- 알수없음: 확실치 않은 가맹점

[출력 형식]
반드시 JSON 리스트 형식으로만 응답하세요. 
오직 [ {{...}}, {{...}} ] 형식의 JSON만 출력하세요.
- source_id: 입력받은 [ID: 번호]의 숫자 (int)
- date: 날짜 (YYYY-MM-DD 형식)
- merchant: 결제처/사용처
- item_name: 품목 요약
- amount: 금액
- category: 카테고리
- status: "Pending" (알수없음 인경우) 또는 "Ready"

입력 데이터:
{combined_texts}
"""
    try:
        model = genai.GenerativeModel('gemini-3.1-flash-lite-preview', generation_config={"response_mime_type": "application/json"})
        response = model.generate_content(prompt)
        
        # 마크다운(```json) 찌꺼기 제거 로직 추가 (LLM 파싱 에러 방지)
        raw_json = response.text.strip()
        if raw_json.startswith("```json"):
            raw_json = raw_json[7:]
        elif raw_json.startswith("```"):
            raw_json = raw_json[3:]
        if raw_json.endswith("```"):
            raw_json = raw_json[:-3]
            
        parsed_data = json.loads(raw_json.strip())
        if isinstance(parsed_data, dict): parsed_data = [parsed_data]
        
        # 사후 처리: 금액 정제 및 UID, 메모리 룰 적용
        rules = st.session_state.memory_rules
        final_list = []
        for p in parsed_data:
            p["amount"] = re.sub(r'[^0-9-]', '', str(p.get("amount", "0")))
            p["uid"] = p.get("uid", uuid.uuid4().hex)
            
            row_id = p.get("source_id")
            if row_id:
                p["_row_num"] = int(row_id)
                p["raw_text"] = raw_text_map.get(int(row_id), "")
            else:
                p["_row_num"] = ""
                p["raw_text"] = ""
                
            merch = p.get("merchant", "")
            if merch in rules:
                p["category"] = rules[merch]
                p["status"] = "Ready"
                
            final_list.append(p)
            
        return final_list
    except Exception as e:
        st.error(f"배치 파싱 에러: {e}")
        return []

def sync_to_notion(data):
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    
    cat = data.get("category", "")
    if cat not in CATEGORY_PAGE_MAP:
        return True, "전송 제외 (자산 이동 또는 수입)"
        
    page_id = CATEGORY_PAGE_MAP[cat]
    
    try:
        amt = float(data.get("amount", "0"))
    except:
        amt = 0.0

    merchant = data.get("merchant", "")
    item_name = data.get("item_name", "")
    title_text = merchant
    if item_name and item_name != '알 수 없음':
        title_text = f"{merchant} ({item_name})"

    date_str = data.get("date", datetime.now().strftime("%Y-%m-%d"))
    
    payload = {
        "parent": {"database_id": CALENDAR_DB_ID},
        "properties": {
            "내역": {"title": [{"text": {"content": title_text}}]},
            "금액": {"number": amt},
            "날짜": {"date": {"start": date_str}},
            "카테고리": {"select": {"name": "지출"}},
            "예산 소비": {"relation": [{"id": page_id}]}
        }
    }
    
    try:
        r = requests.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return True, ""
    except Exception as e:
        return False, str(e)

# --- UI 구성 ---
st.title("☁️ 클라우드 가계부 관제 센터")

records = sheet.get_all_records()
unprocessed = []
for idx, row in enumerate(records):
    row_num = idx + 2 
    status = str(row.get("Status", ""))
    if status != "Synced":
        unprocessed.append((row_num, row))

col1, col2 = st.columns(2)

with col1:
    st.subheader("📡 수신된 원천 데이터 (구글 시트)")
    if unprocessed:
        df = pd.DataFrame([r[1] for r in unprocessed])
        st.dataframe(df)
        
        if st.button("LLM으로 분석하기"):
            st.session_state.parsed_results = []
            
            with st.spinner("제미나이가 데이터(배치)를 열심히 분석 중입니다..."):
                parsed_list = batch_parse_text(unprocessed)
                if parsed_list:
                    st.session_state.parsed_results = parsed_list
                    save_staging_data_safe(st.session_state.parsed_results)
                    st.success("분석 완료! (임시 저장소에 새 데이터로 갱신되었습니다)")
                    st.rerun() # 성공 시에만 리런하여 에러 메세지가 유지되게 함
                else:
                    st.error("파싱에 실패했거나 데이터가 없습니다. 원본 시트나 프롬프트를 확인하세요.")
    else:
        st.info("새로 들어온 데이터가 없습니다.")
        
    st.markdown("---")
    st.subheader("📝 누락 데이터 수동 입력")
    manual_text = st.text_area("결제 문자나 영수증 내용을 복사해서 붙여넣으세요.", height=150)
    if st.button("구글 시트에 추가하기"):
        if manual_text.strip():
            try:
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sheet.append_row([now_str, "Manual", manual_text, ""])
                st.success("수동 입력 데이터가 추가되었습니다! 새로고침(또는 위 목록 확인)을 해주세요.")
            except Exception as e:
                st.error(f"시트 추가 실패: {e}")
        else:
            st.warning("내용을 입력해주세요.")

with col2:
    st.subheader("✅ 노션 전송 대기 (분석 완료)")
    if "parsed_results" in st.session_state and st.session_state.parsed_results:
        pending_items = [p for p in st.session_state.parsed_results if p.get("status") == "Pending" or p.get("category") == "알수없음"]
        ready_items = [p for p in st.session_state.parsed_results if p not in pending_items]
        
        if pending_items:
            st.warning(f"⚠️ {len(pending_items)}건의 알 수 없는 결제처가 있습니다! 카테고리를 지정해주세요.")
            for i, item in enumerate(pending_items):
                with st.container(border=True):
                    merch = item.get('merchant', '')
                    item_name = item.get('item_name', '')
                    display_title = merch
                    if merch in ['쿠팡', '네이버페이', '네이버'] and item_name and item_name != '알 수 없음':
                        display_title = f"{merch} ({item_name})"
                        
                    st.write(f"**{item.get('date')} | {display_title} | {item.get('amount')}원**")
                    with st.expander("결제 원본 및 상세 보기", expanded=False):
                        st.caption(f"품목 요약: {item.get('item_name', '없음')}")
                        st.info(f"원본 문자: {item.get('raw_text', '정보 없음')}")
                    
                    new_cat = st.selectbox(
                        "카테고리 지정", 
                        ["고정지출", "공용 생활비", "주유비", "종호 지출", "혜송 지출", "재이 지출", "여행 문화 쇼핑", "경조사", "차관련비용", "멍게", "수입", "자산 이동", "기타"],
                        key=f"pending_{i}"
                    )
                    
                    col_save_all, col_save_once = st.columns([1, 1])
                    with col_save_all:
                        if st.button("앞으로 이 가맹점은 항상 이렇게 분류", key=f"btn_all_{i}"):
                            st.session_state.memory_rules[item.get("merchant")] = new_cat
                            save_memory_rules(st.session_state.memory_rules)
                            item["category"] = new_cat
                            item["status"] = "Ready"
                            save_staging_data_safe(st.session_state.parsed_results)
                            st.rerun()
                    with col_save_once:
                        if st.button("이번 결제건만 이 카테고리로 저장", key=f"btn_once_{i}"):
                            item["category"] = new_cat
                            item["status"] = "Ready"
                            save_staging_data_safe(st.session_state.parsed_results)
                            st.rerun()

        # UI에서 불필요한 필드는 숨기고 표시
        df_display = pd.DataFrame(st.session_state.parsed_results).drop(columns=["_row_num", "uid", "raw_text"], errors='ignore')
        st.dataframe(df_display)
        
        if st.button("노션으로 최종 전송 (Approve)"):
            st.toast("노션 전송을 시작합니다...", icon="⏳")
            with st.spinner("노션으로 전송 중..."):
                items_to_send = [item for item in st.session_state.parsed_results if item.get("status") == "Ready"]
                remaining_items = [item for item in st.session_state.parsed_results if item.get("status") != "Ready"]
                
                success_row_indices = []
                success_count = 0
                fail_count = 0
                failed_msgs = []
                
                # --- [개선 4] 노션 전송 로직의 원자성 확보 ---
                for item in items_to_send:
                    success, err_msg = sync_to_notion(item)
                    if success:
                        if item.get("_row_num"):
                            success_row_indices.append(item["_row_num"])
                        success_count += 1
                    else:
                        item["status"] = "Fail"
                        remaining_items.append(item)
                        fail_count += 1
                        failed_msgs.append(f"{item.get('merchant')}: {err_msg}")
                
                # 시트 상태 한 번에 업데이트 (Rate Limit 방지)
                if success_row_indices:
                    batch_update_sheet_status(success_row_indices)
                
                st.session_state.parsed_results = remaining_items
                save_staging_data_safe(remaining_items)
                
            if success_count > 0:
                st.toast(f"{success_count}건 노션 전송 성공!", icon="✅")
            if fail_count > 0:
                st.error(f"{fail_count}건 전송 실패:\n" + "\n".join(failed_msgs))
                
            st.success(f"최종 결과: {success_count}건 성공, {fail_count}건 실패 (실패건은 세션에 유지됨)")
            st.rerun()
            
        st.markdown("---")
        st.subheader("📱 텔레그램 일일 브리핑")
        if st.button("📤 오늘 지출 요약 텔레그램 발송 (수동)"):
            with st.spinner("요약 리포트 생성 및 발송 중..."):
                today_str = datetime.now().strftime("%Y-%m-%d")
                today_month_day = today_str[5:]
                
                total_expense = 0
                total_income = 0
                details = []
                
                for item in st.session_state.parsed_results:
                    item_date = item.get("date", "")
                    if today_month_day not in item_date:
                        continue
                        
                    try:
                        amt_str = str(item.get("amount", "0"))
                        amt = int(float(amt_str)) if amt_str else 0
                    except:
                        amt = 0
                        
                    cat = item.get("category", "")
                    if cat == "수입":
                        total_income += amt
                    elif cat not in ["자산 이동", "알수없음"]:
                        total_expense += amt
                        details.append((amt, f"- {cat}: {item.get('merchant')} ({amt:,}원)"))
                
                details.sort(key=lambda x: x[0], reverse=True)
                top3_details = [d[1] for d in details[:3]]
                other_count = len(details) - 3
                
                msg = f"📊 *오늘의 지출 요약 ({today_str})*\n\n"
                msg += f"🔻 총 지출: {total_expense:,}원\n"
                msg += f"🔺 총 수입: {total_income:,}원\n\n"
                msg += "*[가장 큰 지출 Top 3]*\n"
                if top3_details:
                    msg += "\n".join(top3_details)
                    if other_count > 0:
                        msg += f"\n- ...외 {other_count}건"
                else:
                    msg += "- 지출 내역 없음"
                    
                msg += "\n\n🔗 [상세 내역 확인하기](https://cloud-budget-dashboard.streamlit.app/)"
                
                try:
                    bot_token = st.secrets.get("TELEGRAM_TOKEN", "")
                    chat_id = st.secrets.get("TELEGRAM_CHAT_ID", "")
                    if bot_token and chat_id:
                        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                        res = requests.post(url, json={"chat_id": chat_id, "text": msg})
                        if res.status_code == 200:
                            st.success("텔레그램 발송 완료!")
                        else:
                            st.error(f"텔레그램 발송 실패: {res.text}")
                    else:
                        st.warning("발송 완료! (시크릿 미설정)")
                except Exception as e:
                    st.error(f"텔레그램 발송 실패: {e}")
