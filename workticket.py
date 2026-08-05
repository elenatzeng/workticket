import streamlit as st
import requests
from requests.auth import HTTPBasicAuth
import html

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

def fetch_child_pages(domain, email, token, parent_id=None, space_key=None):
    """撈取指定頁面下的子頁面（或根頁面）"""
    auth = HTTPBasicAuth(email, token)
    if parent_id:
        url = f"{domain.rstrip('/')}/wiki/rest/api/content/{parent_id}/child/page"
        params = {"limit": 100}
    else:
        url = f"{domain.rstrip('/')}/wiki/rest/api/content"
        params = {"spaceKey": space_key, "type": "page", "limit": 100, "depth": "root"}
    
    try:
        res = requests.get(url, auth=auth, params=params)
        if res.status_code == 200:
            results = res.json().get('results', [])
            return {p['title']: p['id'] for p in results}
        return {}
    except Exception:
        return {}

def fetch_all_jira_issues_paginated(domain, email, token, sprint_num):
    """
    分頁撈取 Sprint 內的所有工單 (修正 Endpoint 與合法 JQL)
    """
    url = f"{domain.rstrip('/')}/rest/api/3/search"
    auth = HTTPBasicAuth(email, token)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    # 組合多種常見 Sprint 語法 (同時支援 ID 與名稱)
    jql = f'sprint = {sprint_num} OR sprint = "AP Sprint {sprint_num}" OR sprint = "Sprint {sprint_num}" ORDER BY assignee ASC'
    
    all_issues = []
    start_at = 0
    max_results = 100
    
    while True:
        payload = {
            "jql": jql,
            "startAt": start_at,
            "maxResults": max_results,
            "fields": ["summary", "status", "assignee", "timetracking", "issuetype"]
        }
        
        response = requests.post(url, json=payload, headers=headers, auth=auth)
        
        if response.status_code == 200:
            data = response.json()
            issues = data.get('issues', [])
            all_issues.extend(issues)
            
            total = data.get('total', len(all_issues))
            start_at += len(issues)
            
            if start_at >= total or not issues:
                break
        else:
            st.error(f"Jira API 錯誤 ({response.status_code}): {response.text}")
            break
            
    return all_issues

def filter_and_group_by_dept(issues, department):
    """
    僅保留 team_config.py 中指定部門名單內的成員工單
    """
    grouped = {}
    valid_members = TEAM_MEMBERS.get(department, [])
    
    for issue in issues:
        fields = issue.get('fields', {})
        assignee_obj = fields.get('assignee')
        
        if not assignee_obj:
            continue
            
        display_name = assignee_obj.get('displayName', 'Unassigned')
        
        # 比對 displayName 是否包含 team_config.py 該部門成員名字
        is_in_dept = any(member.lower() in display_name.lower() for member in valid_members)
        
        if is_in_dept:
            if display_name not in grouped:
                grouped[display_name] = []
            grouped[display_name].append(issue)
            
    return grouped

def get_status_color(status_name):
    """依據 Jira 狀態名稱轉換為 Confluence Status Macro 色系"""
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
    """估時換算"""
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

def build_confluence_html(grouped_issues, department, current_sprint_num, selected_assignees):
    """建構 Confluence Storage Format XML"""
    header_html = f"""
    <p><strong>{department} 週報會議紀錄</strong></p>
    <ul>
        <li>工作彙報，可包括如下內容
            <ul>
                <li>Sprint 進展</li>
                <li>已制定項目情況</li>
                <li>臨時項目情況</li>
            </ul>
        </li>
        <li>下週計劃</li>
        <li>存在的困難和問題
            <ul>
                <li>跨部門問題</li>
                <li>跨地域問題</li>
                <li>方式方法問題（知識共享、文化、團隊等）</li>
            </ul>
        </li>
    </ul>
    <p><strong>會前務必更新會議紀要：</strong></p>
    <p><strong>會議議程：</strong></p>
    <ol>
        <li>同仁工作彙報
            <ul>
                <li>Sprint 進展（側重問題和困難）</li>
                <li>已制定項目情況</li>
                <li>臨時項目情況</li>
                <li>下週計劃</li>
            </ul>
        </li>
    </ol>
    <hr />
    """
    
    table_rows = ""
    for assignee in selected_assignees:
        issues = grouped_issues.get(assignee, [])
        issues_html = f"<p><u><strong>AP Sprint {current_sprint_num} :</strong></u></p><ul>"
        
        for issue in issues:
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
            
            issues_html += f"<li>{jira_macro}: {html.escape(summary)} {status_macro} {estimate_str}</li>"
            
        issues_html += "</ul>"
        
        next_sprint_num = current_sprint_num + 1
        issues_html += f"""
        <p><u><strong>AP Sprint {next_sprint_num} :</strong></u></p>
        <p><span style="color: rgb(255,102,0);">(這個Sprint未完成，會延至下個Sprint)</span></p>
        """
        
        table_rows += f"""
        <tr>
            <td style="width: 120px; vertical-align: top; font-weight: bold;">{html.escape(assignee)}</td>
            <td style="vertical-align: top;">{issues_html}</td>
        </tr>
        """
        
    table_html = f"""
    <table data-layout="default" style="width: 100%; border-collapse: collapse;" border="1">
        <tbody>
            {table_rows}
        </tbody>
    </table>
    """
    
    return header_html + table_html

def create_confluence_page(domain, email, token, space_key, title, html_content, parent_id=None):
    """發布頁面至 Confluence"""
    url = f"{domain.rstrip('/')}/wiki/rest/api/content"
    auth = HTTPBasicAuth(email, token)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    
    payload = {
        "type": "page",
        "title": title,
        "space": {"key": space_key},
        "body": {
            "storage": {
                "value": html_content,
                "representation": "storage"
            }
        }
    }
    
    if parent_id:
        payload["ancestors"] = [{"id": parent_id}]
        
    return requests.post(url, json=payload, headers=headers, auth=auth)

# -----------------------------------------------------------------------------
# 側邊欄：Atlassian 連線與層級選擇
# -----------------------------------------------------------------------------
with st.sidebar:
    st.header("⚙️ 1. Atlassian 連線設定")
    atlassian_url = st.text_input("Atlassian Domain", value=DEFAULT_DOMAIN)
    api_email = st.text_input("API Email", value=DEFAULT_EMAIL)
    api_token = st.text_input("API Token", type="password", help="請至 https://id.atlassian.com 申請 API Token")
    
    st.divider()
    st.header("📌 2. Confluence 位置選擇")
    
    selected_space_key = None
    selected_parent_id = None
    
    if api_token:
        with st.spinner("連線中..."):
            spaces_dict = fetch_confluence_spaces(atlassian_url, api_email, api_token)
            
        if spaces_dict:
            space_keys = list(spaces_dict.keys())
            default_space_idx = next((i for i, k in enumerate(space_keys) if "QA" in k), 0)
                    
            selected_space_name = st.selectbox("1. 選擇 Confluence Space", space_keys, index=default_space_idx)
            selected_space_key = spaces_dict[selected_space_name]
            
            # 第一層父頁面 (L1)
            root_pages = fetch_child_pages(atlassian_url, api_email, api_token, space_key=selected_space_key)
            if root_pages:
                l1_options = ["(無，存放在 Space 根目錄)"] + list(root_pages.keys())
                idx_l1 = l1_options.index("QA Home") if "QA Home" in l1_options else 0
                selected_l1 = st.selectbox("2. 選擇頂層父頁面 (L1)", l1_options, index=idx_l1)
                
                if selected_l1 != "(無，存放在 Space 根目錄)":
                    selected_parent_id = root_pages[selected_l1]
                    
                    # 第二層父頁面 (L2)
                    l2_pages = fetch_child_pages(atlassian_url, api_email, api_token, parent_id=selected_parent_id)
                    if l2_pages:
                        l2_options = ["(停在此層，做為父頁面)"] + list(l2_pages.keys())
                        idx_l2 = l2_options.index("QA weekly meeting") if "QA weekly meeting" in l2_options else 0
                        selected_l2 = st.selectbox("3. 選擇次層子頁面 (L2)", l2_options, index=idx_l2)
                        
                        if selected_l2 != "(停在此層，做為父頁面)":
                            selected_parent_id = l2_pages[selected_l2]
                            
                            # 第三層父頁面 (L3)
                            l3_pages = fetch_child_pages(atlassian_url, api_email, api_token, parent_id=selected_parent_id)
                            if l3_pages:
                                l3_options = ["(停在此層，做為父頁面)"] + list(l3_pages.keys())
                                idx_l3 = l3_options.index("2026年工作周報") if "2026年工作周報" in l3_options else 0
                                selected_l3 = st.selectbox("4. 選擇第三層子頁面 (L3)", l3_options, index=idx_l3)
                                
                                if selected_l3 != "(停在此層，做為父頁面)":
                                    selected_parent_id = l3_pages[selected_l3]
        else:
            st.error("❌ 連線失敗，請檢查 API Token 是否正確。")
    else:
        st.info("👈 請先輸入 API Token 以載入目錄")

# -----------------------------------------------------------------------------
# 主要表單：生成條件設定
# -----------------------------------------------------------------------------
st.header("1. 選擇生成條件")
col1, col2, col3 = st.columns(3)

with col1:
    department = st.selectbox("部門", list(TEAM_MEMBERS.keys()))

with col2:
    title = st.selectbox("職稱", ["QA Engineer", "Backend Engineer", "Frontend Engineer", "Product Manager"])

with col3:
    sprint_num = st.number_input("Sprint 號碼", min_value=1, max_value=200, value=53)

page_title = f"AP Sprint {sprint_num} {department}週會"

# -----------------------------------------------------------------------------
# 步驟 1：抓取與預覽
# -----------------------------------------------------------------------------
st.header("2. 資料抓取與成員選擇")

if st.button("🔍 1. 從 Jira 抓取工單資料", type="primary"):
    if not api_token:
        st.warning("請先於左側輸入 API Token！")
    else:
        with st.spinner(f"正撈取 Sprint {sprint_num} 的 [{department}] 成員工單..."):
            all_issues = fetch_all_jira_issues_paginated(atlassian_url, api_email, api_token, sprint_num)
            
            if all_issues:
                # 僅篩選出屬於該部門 (例如 QA) 名單中的成員工單
                grouped_dept_issues = filter_and_group_by_dept(all_issues, department)
                
                if grouped_dept_issues:
                    st.session_state.jira_issues = all_issues
                    st.session_state.grouped_issues = grouped_dept_issues
                    st.success(f"🎉 成功撈取！已篩選出 {len(grouped_dept_issues)} 位 [{department}] 成員之工單。")
                else:
                    st.session_state.jira_issues = None
                    st.session_state.grouped_issues = None
                    st.warning(f"在 Sprint {sprint_num} 的工單中，找不到團隊設定檔 (`team_config.py`) 內指定的 [{department}] 成員。")
            else:
                st.session_state.jira_issues = None
                st.session_state.grouped_issues = None
                st.warning(f"在 Jira 中找不到包含 Sprint 號碼為 '{sprint_num}' 的工單。")

# 呈現選擇成員與排版預覽
if st.session_state.grouped_issues:
    dept_assignees = sorted(list(st.session_state.grouped_issues.keys()))

    st.subheader("👥 選擇要上傳/匯出的受託人 (Assignees)")
    selected_assignees = st.multiselect(
        f"目前僅顯示 `team_config.py` 中 [{department}] 的成員，請勾選要發布的人員：",
        options=dept_assignees,
        default=dept_assignees
    )
    
    if selected_assignees:
        confluence_body = build_confluence_html(
            st.session_state.grouped_issues, 
            department, 
            sprint_num, 
            selected_assignees
        )
        
        st.subheader("👁️ 排版預覽 (對齊 Confluence 格式)")
        
        # UI 預覽呈現
        for assignee in selected_assignees:
            issues = st.session_state.grouped_issues.get(assignee, [])
            with st.container():
                st.markdown(f"### **{assignee}**")
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
                st.divider()

        with st.expander("📄 查看原始 Confluence HTML/XML Storage 代碼"):
            st.code(confluence_body, language="xml")
            
        # -----------------------------------------------------------------------------
        # 步驟 2：寫入 Confluence
        # -----------------------------------------------------------------------------
        st.header("3. 發布至 Confluence")
        st.info(f"將建立頁面標題：**{page_title}**")
        
        if st.button("🚀 2. 正式發布頁面至 Confluence", type="secondary"):
            if not selected_space_key:
                st.error("請先於左側選擇 Confluence Space！")
            else:
                with st.spinner("正在發布頁面..."):
                    res = create_confluence_page(
                        domain=atlassian_url,
                        email=api_email,
                        token=api_token,
                        space_key=selected_space_key,
                        title=page_title,
                        html_content=confluence_body,
                        parent_id=selected_parent_id
                    )
                    
                    if res.status_code in [200, 201]:
                        page_data = res.json()
                        web_link = f"{atlassian_url.rstrip('/')}/wiki{page_data.get('_links', {}).get('webui')}"
                        st.balloons()
                        st.success(f"🎉 頁面已成功發布！ [點此前往 Confluence 檢視頁面]({web_link})")
                    else:
                        st.error(f"Confluence 發布失敗 ({res.status_code}): {res.text}")
