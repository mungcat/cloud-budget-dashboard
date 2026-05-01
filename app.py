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

st.set_page_config(page_title="Cloud Budget Manager", layout="wide")

# --- 중간 저장소 (Google Sheets) 로드/저장 ---
def get_parsed_sheet():
    try:
        return client.open(SHEET_NAME).worksheet("Budget_Parsed")
    except gspread.exceptions.WorksheetNotFound:
        new_sh = client.open(SHEET_NAME).add_worksheet(title="Budget_Parsed", rows="1000", cols="10")
        new_sh.append_row(["date", "merchant", "item_name", "amount", "category", "status", "raw_text", "_row_num"])
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
        records = get_parsed_sheet().get_all_records()
        return records
    except:
        return []

def save_staging_data(data_list):
    try:
        sh = get_parsed_sheet()
        sh.clear()
        sh.append_row(["date", "merchant", "item_name", "amount", "category", "status", "raw_text", "_row_num"])
        if data_list:
            rows = []
            for d in data_list:
                rows.append([
                    str(d.get("date", "")), str(d.get("merchant", "")), str(d.get("item_name", "")),
                    str(d.get("amount", "")), str(d.get("category", "")), str(d.get("status", "")),
                    str(d.get("raw_text", "")), str(d.get("_row_num", ""))
                ])
            sh.append_rows(rows)
    except Exception as e:
        st.error(f"저장 실패: {e}")

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
    except Exception as e:
        pass

if "parsed_results" not in st.session_state:
    st.session_state.parsed_results = load_staging_data()
if "memory_rules" not in st.session_state:
    st.session_state.memory_rules = load_memory_rules()

# --- 보안: 간단한 로그인 로직 ---
if "authenticated" not in st.session_state:
    # URL 파라미터를 이용한 자동 로그인 기능 (?pw=554477 로 접속 시 자동 통과)
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
                # 성공 시 자동 로그인을 위한 파라미터 세팅
                st.query_params.pw = "554477"
                st.rerun()
            else:
                st.error("아이디 또는 비밀번호가 틀렸습니다.")

if not st.session_state.authenticated:
    check_login()
    st.stop()

# --- 설정 및 인증 (Streamlit Secrets 사용) ---
try:
    gcp_creds = dict(st.secrets["gcp_service_account"])
    GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
    NOTION_TOKEN = st.secrets["NOTION_TOKEN"]
    NOTION_DATABASE_ID = st.secrets["NOTION_DATABASE_ID"]
except Exception as e:
    st.error("Streamlit Secrets 설정이 누락되었습니다. 깃허브 업로드 후 세팅해주세요!")
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
SHEET_NAME = "Budget_SMS_Receiver"
try:
    sheet = client.open(SHEET_NAME).sheet1
except Exception:
    st.error(f"'{SHEET_NAME}' 구글 시트를 찾을 수 없습니다.")
    st.stop()

# --- 헬퍼 함수 ---
def parse_text(raw_text):
    prompt = f"""
다음 결제 문자/텍스트를 분석하여 JSON 배열로 반환하세요.
[노션 카테고리 분류 기준]
- 고정지출: 관리비, 대출이자, 보험료, 정기구독
- 종호 지출: SK세븐모바일, 블루멤버스 등 남편 개인 지출 및 핸드폰 요금
- 혜송 지출: 아내 개인 지출
- 공용 생활비: 식비, 마트 등
- 주유비: 주유소
- 재이 지출: 아이사랑 카드, 유아용품
- 여행 문화 쇼핑: 쇼핑몰, 숙박, 여행
- 경조사: 축의금, 부조금
- 자산 이동: 쿠페이 충전, 네이버페이 충전 등 선불페이 지갑으로 돈이 넘어가는 것
- 수입: 입금, 월급 등
- 알수없음: 확실치 않은 가맹점

[🔥네이버페이/쿠페이 중복 결제 특수 로직🔥]
결제 수단이 '네이버페이'나 '쿠페이'인 경우, 은행 계좌에서 돈이 빠져나가는 출금 문자와 실제 쇼핑몰 결제 내역이 중복될 가능성이 매우 높습니다.
1. 은행 출금 데이터(예: SC제일은행 -> 네이버페이 출금 65,275원)는 무조건 카테고리를 '자산 이동'으로 고정하고, 실소비(지출)로 취급하지 마세요.
2. 실제 소비 금액은 반드시 쇼핑몰 상세 내역(예: 베어블리 자석블록 36,900원)의 품목별 금액을 합산해서 산출하고, 이를 '실소비' 항목(재이 지출 등)으로 분류하세요.
3. 네이버쇼핑 내역 중 상품명(item_name)이 있는 상세 데이터가 들어오면 최대한 그 상품명에 맞춰 카테고리를 추론하세요.

오직 [ {{...}} ] 형식의 JSON만 출력하세요.
- date: 날짜 (YYYY-MM-DD 형식)
- merchant: 결제처/사용처
- item_name: 품목 (문자 원본 내용 요약)
- amount: 금액 (숫자, 취소면 음수)
- category: 카테고리
- status: "Pending" (알수없음 인경우) 또는 "Ready"
- raw_text: 문자 원본 전체 내용

텍스트:
{raw_text}
"""
    try:
        model = genai.GenerativeModel('gemini-3.1-flash-lite-preview')
        res = model.generate_content(prompt).text.strip()
        
        # 앞뒤 마크다운 찌꺼기 및 쓸데없는 말 제거 로직 강화
        match = re.search(r'(\[.*\]|\{.*\})', res, re.DOTALL)
        if match:
            res = match.group(1)
            
        parsed = json.loads(res)
        parsed_list = parsed if isinstance(parsed, list) else [parsed]
        
        # LLM 메모리 규칙 적용
        rules = st.session_state.memory_rules
        for p in parsed_list:
            merch = p.get("merchant", "")
            if merch in rules:
                p["category"] = rules[merch]
                p["status"] = "Ready" # 메모리에 있으면 무조건 Ready
                
        return parsed_list
    except Exception as e:
        st.error(f"제미나이 파싱 에러 (JSON 변환 실패): {e}")
        return []

def sync_to_notion(data):
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }
    
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
    
    cat = data.get("category", "")
    if cat not in CATEGORY_PAGE_MAP:
        return True, "전송 제외 (자산 이동 또는 수입)"
        
    page_id = CATEGORY_PAGE_MAP[cat]
    
    try:
        amt_str = str(data.get("amount", "0")).replace(",", "").replace("원", "").strip()
        amt = float(amt_str)
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
        err_msg = r.text if 'r' in locals() else str(e)
        return False, err_msg

# --- UI 구성 ---
st.title("☁️ 클라우드 가계부 관제 센터")

records = sheet.get_all_records()

# 구글 시트에 "Status" 컬럼이 없으면 빈 값으로 취급
unprocessed = []
for idx, row in enumerate(records):
    # row index in gspread starts from 2 (1 is header)
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
            if "parsed_results" not in st.session_state:
                st.session_state.parsed_results = []
            
            # 이미 분석된 row_num 추출 (중복 분석 방지)
            already_parsed_rows = {p.get("_row_num") for p in st.session_state.parsed_results if p.get("_row_num")}
            
            with st.spinner("제미나이가 열심히 분석 중입니다..."):
                for row_num, row in unprocessed:
                    if row_num in already_parsed_rows:
                        continue # 이미 스테이징에 있으면 스킵
                        
                    text = str(row.get("Text", row.get("text", "")))
                    parsed_list = parse_text(text)
                    for p in parsed_list:
                        p["_row_num"] = row_num # 나중에 Status 업데이트용
                        p["raw_text"] = text # 원본 텍스트 추가
                        st.session_state.parsed_results.append(p)
                
                # 분석 완료 후 중간 저장소(JSON)에 덮어쓰기/추가
                save_staging_data(st.session_state.parsed_results)
            st.success("분석 완료! (임시 저장소에 안전하게 보관되었습니다)")
            st.rerun()
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
        # 상태가 Pending인 항목들 필터링
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
                            save_staging_data(st.session_state.parsed_results)
                            st.rerun()
                    with col_save_once:
                        if st.button("이번 결제건만 이 카테고리로 저장", key=f"btn_once_{i}"):
                            item["category"] = new_cat
                            item["status"] = "Ready"
                            save_staging_data(st.session_state.parsed_results)
                            st.rerun()

        df_parsed = pd.DataFrame(st.session_state.parsed_results).drop(columns=["_row_num"], errors='ignore')
        st.dataframe(df_parsed)
        
        if st.button("노션으로 최종 전송 (Approve)"):
            st.toast("노션 전송을 시작합니다...", icon="⏳")
            with st.spinner("노션으로 전송 중..."):
                success_count = 0
                fail_count = 0
                failed_items = []
                
                # Ready 상태인 항목만 필터링해서 전송 (staging 중복 방지)
                items_to_send = [item for item in st.session_state.parsed_results if item.get("status") == "Ready"]
                items_to_keep = [item for item in st.session_state.parsed_results if item.get("status") != "Ready"]
                
                for item in items_to_send:
                    success, err_msg = sync_to_notion(item)
                    if success:
                        success_count += 1
                        try:
                            sheet.update_cell(item["_row_num"], 4, "Synced")
                        except:
                            pass
                    else:
                        fail_count += 1
                        failed_items.append(f"{item.get('merchant')}: {err_msg}")
                        items_to_keep.append(item) # 실패한 건 유지
                
                # 상태 업데이트 (성공한건 빼고, 실패/대기중인건 남김)
                st.session_state.parsed_results = items_to_keep
                save_staging_data(items_to_keep)
                
            if success_count > 0:
                st.toast(f"{success_count}건 노션 전송 성공!", icon="✅")
            if fail_count > 0:
                st.error(f"{fail_count}건 전송 실패:\n" + "\n".join(failed_items))
                
            st.success(f"최종 결과: {success_count}건 성공, {fail_count}건 실패")
            st.rerun()
            
        st.markdown("---")
        st.subheader("📱 텔레그램 일일 브리핑")
        if st.button("📤 오늘 지출 요약 텔레그램 발송 (수동)"):
            with st.spinner("요약 리포트 생성 및 발송 중..."):
                # 오늘 지출/수입 계산
                today_str = datetime.now().strftime("%Y-%m-%d")
                today_month_day = today_str[5:] # "05-01" 형식 추출 (연도 무시 테스트용)
                
                total_expense = 0
                total_income = 0
                details = []
                
                for item in st.session_state.parsed_results:
                    # 오늘 날짜(월-일)가 포함된 결제건만 필터링
                    item_date = item.get("date", "")
                    if today_month_day not in item_date:
                        continue
                        
                    try:
                        amt_str = str(item.get("amount", "0")).replace(",", "").replace("원", "").strip()
                        amt = int(float(amt_str))
                    except:
                        amt = 0
                        
                    cat = item.get("category", "")
                    if cat == "수입":
                        total_income += amt
                    elif cat not in ["자산 이동", "알수없음"]:
                        total_expense += amt
                        details.append((amt, f"- {cat}: {item.get('merchant')} ({amt:,}원)"))
                
                # 금액 기준 Top 3 추출
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
                    
                msg += "\n\n🔗 [상세 내역 확인하기](https://cloud-budget-dashboard-h4cjdtlvkrpvc2nyl5lbkd.streamlit.app/)"
                
                # 텔레그램 API 호출 (Secrets에 TELEGRAM_TOKEN, CHAT_ID 필요)
                try:
                    bot_token = st.secrets.get("TELEGRAM_TOKEN", "")
                    chat_id = st.secrets.get("TELEGRAM_CHAT_ID", "")
                    
                    if bot_token and chat_id:
                        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                        # Markdown 에러 방지를 위해 일단 순수 텍스트로 보냄
                        res = requests.post(url, json={"chat_id": chat_id, "text": msg})
                        if res.status_code == 200:
                            st.success("텔레그램 발송 완료!")
                        else:
                            st.error(f"텔레그램 발송 실패: {res.text}")
                    else:
                        st.warning("발송 완료! (다만 시크릿에 TELEGRAM_TOKEN/CHAT_ID가 없어 콘솔에만 출력됩니다.)")
                        st.code(msg)
                except Exception as e:
                    st.error(f"텔레그램 발송 실패: {e}")
    else:
        st.info("분석 대기 중인 데이터가 없습니다.")
        st.markdown("---")
        if st.button("📤 텔레그램 수동 보고하기 (임시)"):
            st.info("아직 텔레그램 봇 토큰이 연결되지 않았습니다. (다음 단계에서 구현 예정)")
