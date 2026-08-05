import streamlit as st
import requests
from requests.auth import HTTPBasicAuth
import html
import re
from bs4 import BeautifulSoup

# 匯入團隊組織設定檔
from team_config import TEAM_MEMBERS

st.set_page_config(page_title="WorkTicket - Jira to Confluence 工具", layout="wide", page_icon="📋")

st.title("📋 WorkTicket: Jira to Confluence 週會頁面自動生成器")

# 預設網域與設定
DEFAULT_DOMAIN = "https://inta.atlassian.net/"
DEFAULT_EMAIL = st.secrets.get("ATLASSIAN_EMAIL", "ela@intellianalyze.com")

# Session State 初始化
if "jira_issues" not in st.session_state:
    st.session_state.jira_issues = None
if "grouped_issues" not in st.session_state:
    st.session_state.grouped_issues = None

# -----------------------------------------------------------------------------
# 輔助 API 函式
# -----------------------------------------------------------------------------
def fetch_confluence_spaces(domain, email, token):
    """取得 Confluence 所有團隊 Space 清單"""
    url = f"{domain.rstrip('/')}/wiki/rest/api/space"
    auth = HTTPBasicAuth(email, token)
    params = {"limit": 100, "type": "global", "status": "current"}
    try:
        res = requests.get(url, auth=auth, params=params)
        if res.status_code == 200:
            results = res.json().get('results', [])
            spaces_dict = {}
            for s in results:
                if s.get('type') != 'personal' and not s.get('key', '').startswith('~'):
                    spaces_dict[f"{s['name']} ({s['key']})"] = s['key']
            return spaces_dict
        return {}
    except Exception:
        return {}

def fetch_all_active_jira_issues(domain, email, token):
    """使用 Jira v3 JQL 端點撈取進行中工單"""
    auth = HTTPBasicAuth(email, token)
    jql = 'statusCategory in ("To Do", "In Progress") ORDER BY assignee ASC'

    url = f"{domain.rstrip('/')}/rest/api/3/search/jql"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    all_issues = []
    next_page_token = None

    while True:
        payload = {
            "jql": jql,
            "maxResults": 100,
            "fields": ["summary", "status", "assignee", "timetracking", "issuetype", "customfield_10020"]
        }
        if next_page_token:
            payload["nextPageToken"] = next_page_token

        response = requests.post(url, json=payload, headers=headers, auth=auth)

        if response.status_code != 200:
            st.error(f"Jira API 錯誤 ({response.status_code}): {response.text}")
            break

        data = response.json()
        issues = data.get('issues', [])
        all_issues.extend(issues)

        next_page_token = data.get('nextPageToken')
        is_last = data.get('isLast', True)

        if not next_page_token or is_last or not issues:
            break

    return all_issues

def filter_and_group_by_dept(issues, department, sprint_num):
    """根據成員與 Sprint 比對工單"""
    grouped = {}
    valid_members = TEAM_MEMBERS.get(department, [])
    
    member_keywords = []
    for m in valid_members:
        if isinstance(m, dict):
            if m.get('name'): member_keywords.append(m['name'].lower())
            if m.get('email'): member_keywords.append(m['email'].lower())
        elif isinstance(m, str):
            member_keywords.append(m.lower())

    sprint_str = str(sprint_num)
    debug_found_assignees = set()

    for issue in issues:
        fields = issue.get('fields', {})
        assignee_obj = fields.get('assignee')
        
        if not assignee_obj:
            continue
            
        display_name = assignee_obj.get('displayName', '')
        email_address = assignee_obj.get('emailAddress', '').lower()
        
        if display_name:
            debug_found_assignees.add(display_name)
            
        is_in_dept = False
        matched_display_name = display_name or email_address
        
        for key in member_keywords:
            if (display_name and key in display_name.lower()) or (email_address and key in email_address):
                is_in_dept = True
                break
                
        issue_raw_str = str(issue)
        has_sprint_match = (sprint_str in issue_raw_str)
        
        if is_in_dept and has_sprint_match:
            if matched_display_name not in grouped:
                grouped[matched_display_name] = []
            grouped[matched_display_name].append(issue)
            
    return grouped, debug_found_assignees

def get_status_color(status_name):
    """狀態標籤顏色"""
    status_upper = status_name.upper()
    if status_upper in ["DONE", "RESOLVED", "CLOSED"]:
        return "Green"
    elif status_upper in ["IN PROGRESS", "IN DEVELOPMENT"]:
        return "Blue"
    elif "PENDING" in status_upper or "TESTING" in status_upper:
        return "Yellow"
    else:
        return "Grey"

def format_estimate(timetracking):
    """估時格式化"""
    if not timetracking:
        return "待評估"
    original_estimate = timetracking.get('originalEstimate')
    if original_estimate:
        return f"估時：{original_estimate}"
    seconds = timetracking.get('originalEstimateSeconds')
    if seconds:
        days = round(seconds / (8 * 3600), 1)
        if days.is_integer():
            days = int(days)
        return f"估時：{days}D"
    return "待評估"

def build_single_issue_li_html(issue):
    """建構單一工單的 <li> HTML"""
    key = issue.get('key')
    fields = issue.get('fields', {})
    summary = fields.get('summary', '')
    status_obj = fields.get('status', {})
    status_name = status_obj.get('name', 'To Do')
    status_color = get_status_color(status_name)
    timetracking = fields.get('timetracking', {})
    estimate_str = format_estimate(timetracking)
    
    jira_macro = f'<ac:structured-macro ac:name="jira" ac:schema-version="1"><ac:parameter ac:name="key">{key}</ac:parameter></ac:structured-macro>'
    status_macro = f'<ac:structured-macro ac:name="status" ac:schema-version="1"><ac:parameter ac:name="title">{html.escape(status_name)}</ac:parameter><ac:parameter ac:name="colour">{status_color}</ac:parameter></ac:structured-macro>'
    
    return f"<li>{jira_macro}: {html.escape(summary)} {status_macro} {estimate_str}</li>"

def generate_user_full_html(issues, current_sprint_num):
    """生成單一名成員完整的 HTML（供複製用）"""
    issues_html = f"<p><u><strong>AP Sprint {current_sprint_num} :</strong></u></p><ul>"
    for issue in issues:
        issues_html += build_single_issue_li_html(issue)
    issues_html += "</ul>"
    
    next_sprint_num = current_sprint_num + 1
    issues_html += f"""
    <p><u><strong>AP Sprint {next_sprint_num} :</strong></u></p>
    <p><span style="color: rgb(255,102,0);">(這個Sprint未完成，會延至下個Sprint)</span></p>
    """
    return issues_html

def update_confluence_page_by_user(domain, email, token, space_key, title, grouped_issues, selected_assignees, current_sprint_num):
    """對選定之同仁進行比對與增補更新"""
    auth = HTTPBasicAuth(email, token)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    
    search_url = f"{domain.rstrip('/')}/wiki/rest/api/content"
    params = {
        "spaceKey": space_key,
        "title": title,
        "expand": "body.storage,version"
    }
    
    res = requests.get(search_url, params=params, auth=auth)
    if res.status_code != 200 or not res.json().get('results'):
        return False, f"找不到名稱為『{title}』的頁面，請確認標題與 Space 選擇是否正確。"
        
    page_data = res.json()['results'][0]
    page_id = page_data['id']
    current_version = page_data['version']['number']
    original_html = page_data['body']['storage']['value']
    
    soup = BeautifulSoup(original_html, 'html.parser')
    updated_count = 0
    added_issues_count = 0
    
    rows = soup.find_all('tr')
    for row in rows:
        cols = row.find_all('td')
        if len(cols) >= 2:
            first_col_text = cols[0].get_text().strip()
            
            for assignee in selected_assignees:
                if assignee.lower() in first_col_text.lower():
                    updated_count += 1
                    target_td = cols[1]
                    
                    td_raw_str = str(target_td)
                    existing_keys = set(re.findall(r'[A-Z0-9]+-\d+', td_raw_str))
                    
                    latest_user_issues = grouped_issues.get(assignee, [])
                    new_issues_to_add = [i for i in latest_user_issues if i.get('key') not in existing_keys]
                    
                    if new_issues_to_add:
                        target_ul = target_td.find('ul')
                        if not target_ul:
                            target_ul = soup.new_tag('ul')
                            target_td.append(target_ul)
                            
                        for new_issue in new_issues_to_add:
                            li_html = build_single_issue_li_html(new_issue)
                            new_li_soup = BeautifulSoup(li_html, 'html.parser')
                            target_ul.append(new_li_soup)
                            added_issues_count += 1
                    break

    if updated_count == 0:
        return False, f"未能比對到表格中的成員名稱：{', '.join(selected_assignees)}"

    if added_issues_count == 0:
        return True, "沒有新工單需要補充，所有項目皆已存在。"

    updated_storage_html = str(soup)
    update_url = f"{domain.rstrip('/')}/wiki/rest/api/content/{page_id}"
    
    payload = {
        "version": {"number": current_version + 1},
        "title": title,
        "type": "page",
        "body": {
            "storage": {
                "value": updated_storage_html,
                "representation": "storage"
            }
        }
    }
    
    put_res = requests.put(update_url, json=payload, headers=headers, auth=auth)
    if put_res.status_code == 200:
        web_link = f"{domain.rstrip('/')}/wiki{put_res.json().get('_links', {}).get('webui')}"
        return True, f"成功幫 {', '.join(selected_assignees)} 補上 {added_issues_count} 筆新工單！ [點此前往檢視頁面]({web_link})"
    else:
        return False, f"寫入 Confluence 失敗 ({put_res.status_code}): {put_res.text}"

# -----------------------------------------------------------------------------
# 側邊欄與頁面配置
# -----------------------------------------------------------------------------
with st.sidebar:
    st.header("⚙️ 1. Atlassian 連線設定")
    atlassian_url = st.text_input("Atlassian Domain", value=DEFAULT_DOMAIN)
    api_email = st.text_input("API Email", value=DEFAULT_EMAIL)
    api_token = st.text_input("API Token", type="password")
    
    st.divider()
    st.header("📌 2. Confluence 位置選擇")
    
    selected_space_key = None
    if api_token:
        spaces_dict = fetch_confluence_spaces(atlassian_url, api_email, api_token)
        if spaces_dict:
            space_keys = list(spaces_dict.keys())
            default_space_idx = next((i for i, k in enumerate(space_keys) if "QA" in k), 0)
            selected_space_name = st.selectbox("1. 選擇 Confluence Space", space_keys, index=default_space_idx)
            selected_space_key = spaces_dict[selected_space_name]

# 主要區域
st.header("1. 選擇生成與更新條件")
col1, col2, col3 = st.columns(3)

with col1:
    department = st.selectbox("部門", list(TEAM_MEMBERS.keys()))
with col2:
    title = st.selectbox("職稱", ["QA Engineer", "Backend Engineer", "Frontend Engineer", "Product Manager"])
with col3:
    sprint_num = st.number_input("Sprint 號碼", min_value=1, max_value=200, value=52)

target_page_title = st.text_input("要補充工單的 Confluence 頁面標題", value=f"Sprint {sprint_num} {department}週會")

st.header("2. 資料抓取與成員選擇")

if st.button("🔍 從 Jira 抓取工單資料", type="primary"):
    if not api_token:
        st.warning("請先於左側輸入 API Token！")
    else:
        with st.spinner(f"正檢索 Jira 中包含 Sprint {sprint_num} 的 [{department}] 工單..."):
            all_issues = fetch_all_active_jira_issues(atlassian_url, api_email, api_token)
            if all_issues:
                grouped_dept_issues, debug_found_assignees = filter_and_group_by_dept(all_issues, department, sprint_num)
                if grouped_dept_issues:
                    st.session_state.jira_issues = all_issues
                    st.session_state.grouped_issues = grouped_dept_issues
                    st.success(f"🎉 成功抓取！已準備好 {len(grouped_dept_issues)} 位同仁的最新工單。")
                else:
                    st.warning(f"已抓取到工單，但未比對到 Sprint {sprint_num} 的資料。")
            else:
                st.warning("無法取得工單。")

if st.session_state.grouped_issues:
    dept_assignees = sorted(list(st.session_state.grouped_issues.keys()))
    
    st.divider()
    st.subheader("👥 批次操作區")
    
    selected_assignees = st.multiselect("勾選成員進行批次更新：", options=dept_assignees, default=dept_assignees)
    
    if st.button("🚀 批次增補更新所有勾選成員至 Confluence", type="secondary"):
        if not selected_space_key:
            st.error("請先選擇 Space！")
        else:
            with st.spinner("正在批次比對與補充工單..."):
                success, msg = update_confluence_page_by_user(
                    domain=atlassian_url,
                    email=api_email,
                    token=api_token,
                    space_key=selected_space_key,
                    title=target_page_title,
                    grouped_issues=st.session_state.grouped_issues,
                    selected_assignees=selected_assignees,
                    current_sprint_num=sprint_num
                )
                if success:
                    st.balloons()
                    st.success(msg)
                else:
                    st.error(msg)

    st.divider()
    st.subheader("👤 個人排版預覽與單獨更新 / 複製區")

    # 針對每一位成員獨立開立操作按鈕區
    for assignee in dept_assignees:
        issues = st.session_state.grouped_issues.get(assignee, [])
        user_html = generate_user_full_html(issues, sprint_num)
        
        with st.expander(f"👤 {assignee} ({len(issues)} 筆工單)", expanded=True):
            col_left, col_right = st.columns([3, 1])
            
            with col_left:
                st.markdown(f"<u>**AP Sprint {sprint_num} :**</u>", unsafe_allow_html=True)
                for issue in issues:
                    key = issue.get('key')
                    fields = issue.get('fields', {})
                    summary = fields.get('summary', '')
                    status_name = fields.get('status', {}).get('name', 'To Do')
                    estimate_str = format_estimate(fields.get('timetracking', {}))
                    st.markdown(f"- **[{key}]** {summary} ` {status_name} ` {estimate_str}")
                
                st.markdown(f"<u>**AP Sprint {sprint_num + 1} :**</u>", unsafe_allow_html=True)
                st.markdown("<span style='color: #FF6600;'>(這個Sprint未完成，會延至下個Sprint)</span>", unsafe_allow_html=True)
            
            with col_right:
                st.write("**單獨操作**")
                
                # 1. 單獨更新按鈕
                if st.button(f"🚀 僅更新 {assignee}", key=f"btn_update_{assignee}"):
                    if not selected_space_key:
                        st.error("請先選擇 Space！")
                    else:
                        with st.spinner(f"更新 {assignee} 中..."):
                            success, msg = update_confluence_page_by_user(
                                domain=atlassian_url,
                                email=api_email,
                                token=api_token,
                                space_key=selected_space_key,
                                title=target_page_title,
                                grouped_issues=st.session_state.grouped_issues,
                                selected_assignees=[assignee],
                                current_sprint_num=sprint_num
                            )
                            if success:
                                st.success(msg)
                            else:
                                st.error(msg)
                
                # 2. 複製 HTML 代碼按鈕 (使用 st.code 原生複製功能)
                st.caption("點擊右上方按鈕複製 HTML：")
                st.code(user_html, language="html")
