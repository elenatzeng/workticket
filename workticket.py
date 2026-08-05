import streamlit as st
import requests
from requests.auth import HTTPBasicAuth
import html

st.set_page_config(page_title="WorkTicket - Jira to Confluence 工具", layout="wide", page_icon="📋")

st.title("📋 WorkTicket: Jira to Confluence 週會頁面自動生成器")

# 讀取預設設定
default_domain = st.secrets.get("ATLASSIAN_DOMAIN", "https://your-domain.atlassian.net")
default_email = st.secrets.get("ATLASSIAN_EMAIL", "your-email@example.com")

# -----------------------------------------------------------------------------
# 輔助 API 函式：連線與選單撈取
# -----------------------------------------------------------------------------
def fetch_confluence_spaces(domain, email, token):
    """取得 Confluence 所有團隊/組織 Space 清單（排除個人空間）"""
    url = f"{domain.rstrip('/')}/wiki/rest/api/space"
    auth = HTTPBasicAuth(email, token)
    params = {
        "limit": 100,
        "type": "global",
        "status": "current"
    }
    try:
        res = requests.get(url, auth=auth, params=params)
        if res.status_code == 200:
            results = res.json().get('results', [])
            spaces_dict = {}
            for s in results:
                # 過濾：排除 type 為 personal 或 Key 為 ~ 開頭的個人空間
                if s.get('type') != 'personal' and not s.get('key', '').startswith('~'):
                    spaces_dict[f"{s['name']} ({s['key']})"] = s['key']
            return spaces_dict
        return {}
    except Exception:
        return {}

def fetch_confluence_pages(domain, email, token, space_key):
    """取得指定 Space 下的頁面清單，用來選取父頁面"""
    url = f"{domain.rstrip('/')}/wiki/rest/api/content"
    auth = HTTPBasicAuth(email, token)
    params = {
        "spaceKey": space_key,
        "type": "page",
        "limit": 100,
        "expand": "version"
    }
    try:
        res = requests.get(url, auth=auth, params=params)
        if res.status_code == 200:
            results = res.json().get('results', [])
            return {p['title']: p['id'] for p in results}
        return {}
    except Exception:
        return {}

def fetch_jira_issues(domain, email, token, sprint):
    """根據 Sprint 名稱從 Jira 撈取 Issue"""
    url = f"{domain.rstrip('/')}/rest/api/3/search"
    auth = HTTPBasicAuth(email, token)
    headers = {"Accept": "application/json"}
    
    jql = f'sprint = "{sprint}" ORDER BY assignee ASC'
    params = {
        'jql': jql,
        'maxResults': 100,
        'fields': 'summary,status,assignee,timetracking,issuetype'
    }
    
    response = requests.get(url, headers=headers, params=params, auth=auth)
    if response.status_code == 200:
        return response.json().get('issues', [])
    else:
        st.error(f"Jira API 錯誤 ({response.status_code}): {response.text}")
        return []

def group_issues_by_assignee(issues):
    """將 Issue 依據經辦人 (Assignee) 分組"""
    grouped = {}
    for issue in issues:
        fields = issue.get('fields', {})
        assignee_obj = fields.get('assignee')
        assignee_name = assignee_obj.get('displayName', 'Unassigned') if assignee_obj else 'Unassigned'
        
        if assignee_name not in grouped:
            grouped[assignee_name] = []
        grouped[assignee_name].append(issue)
    return grouped

def build_confluence_html(grouped_issues, department, current_sprint, next_sprint):
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
    for assignee, issues in grouped_issues.items():
        issues_html = f"<p><strong>AP {current_sprint} :</strong></p><ul>"
        
        for issue in issues:
            key = issue.get('key')
            jira_macro = f'<ac:structured-macro ac:name="jira" ac:schema-version="1"><ac:parameter ac:name="key">{key}</ac:parameter></ac:structured-macro>'
            issues_html += f"<li>{jira_macro}</li>"
            
        issues_html += "</ul>"
        
        issues_html += f"""
        <p><strong>AP {next_sprint} :</strong></p>
        <p><span style="color: rgb(255,102,0);">(這個Sprint未完成，會延至下個Sprint)</span></p>
        """
        
        table_rows += f"""
        <tr>
            <td style="width: 150px; vertical-align: top;"><strong>{html.escape(assignee)}</strong></td>
            <td>{issues_html}</td>
        </tr>
        """
        
    table_html = f"""
    <table data-layout="default">
        <tbody>
            {table_rows}
        </tbody>
    </table>
    """
    
    return header_html + table_html

def create_confluence_page(domain, email, token, space_key, title, html_content, parent_id=None):
    """發布新頁面到 Confluence"""
    url = f"{domain.rstrip('/')}/wiki/rest/api/content"
    auth = HTTPBasicAuth(email, token)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
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
        
    response = requests.post(url, json=payload, headers=headers, auth=auth)
    return response

# -----------------------------------------------------------------------------
# 側邊欄：Atlassian 連線與動態選單（支援搜尋過濾）
# -----------------------------------------------------------------------------
with st.sidebar:
    st.header("⚙️ 1. Atlassian 連線設定")
    atlassian_url = st.text_input("Atlassian Domain", value=default_domain)
    api_email = st.text_input("API Email", value=default_email)
    api_token = st.text_input("API Token", type="password", help="請至 https://id.atlassian.com 申請 API Token")
    
    st.divider()
    st.header("📌 2. Confluence 位置選擇")
    
    selected_space_key = None
    selected_parent_id = None
    
    if api_token:
        with st.spinner("連線中，撈取 Space 清單..."):
            spaces_dict = fetch_confluence_spaces(atlassian_url, api_email, api_token)
            
        if spaces_dict:
            # 1. Space 搜尋過濾
            space_search = st.text_input("🔍 搜尋 Space 名稱或 Key", value="", help="輸入關鍵字即可過濾下方 Space 選單")
            
            if space_search.strip():
                filtered_spaces = {
                    name: key for name, key in spaces_dict.items()
                    if space_search.lower() in name.lower()
                }
            else:
                filtered_spaces = spaces_dict
                
            if filtered_spaces:
                selected_space_name = st.selectbox("選擇 Confluence Space", list(filtered_spaces.keys()))
                selected_space_key = filtered_spaces[selected_space_name]
                
                # 2. 撈取父頁面清單
                with st.spinner("撈取父頁面清單..."):
                    pages_dict = fetch_confluence_pages(atlassian_url, api_email, api_token, selected_space_key)
                    
                if pages_dict:
                    # Parent Page 搜尋過濾
                    page_search = st.text_input("🔍 搜尋父頁面標題", value="", help="輸入關鍵字過濾父頁面")
                    
                    if page_search.strip():
                        filtered_pages = {
                            title: pid for title, pid in pages_dict.items()
                            if page_search.lower() in title.lower()
                        }
                    else:
                        filtered_pages = pages_dict
                        
                    page_options = ["(不指定，直接存放在 Space 根目錄)"] + list(filtered_pages.keys())
                    selected_page_name = st.selectbox("選擇父頁面 (Parent Page)", page_options)
                    
                    if selected_page_name != "(不指定，直接存放在 Space 根目錄)":
                        selected_parent_id = filtered_pages[selected_page_name]
                else:
                    st.caption("⚠️ 該 Space 下找不到可選的頁面或無權限。")
            else:
                st.warning("⚠️ 找不到符合條件的 Space。")
        else:
            st.error("❌ 連線失敗，請檢查 Domain, Email 或 API Token 是否正確。")
    else:
        st.info("👈 請先輸入 API Token 以載入 Space 與父頁面選單")

# -----------------------------------------------------------------------------
# 主要表單：部門、職稱與 Sprint 選擇
# -----------------------------------------------------------------------------
st.header("1. 選擇生成條件")
col1, col2, col3 = st.columns(3)

with col1:
    department = st.selectbox("部門", ["QA", "RD", "PM", "Design"])

with col2:
    title = st.selectbox("職稱", ["QA Engineer", "Backend Engineer", "Frontend Engineer", "Product Manager"])

with col3:
    sprint_num = st.number_input("Sprint 號碼", min_value=1, max_value=200, value=53)

sprint_name = f"Sprint {sprint_num}"
page_title = f"{sprint_name} {department}週會"

# -----------------------------------------------------------------------------
# 預覽與執行發布
# -----------------------------------------------------------------------------
st.header("2. 預覽與寫入")

if st.button("🚀 從 Jira 抓取資料並生成頁面", type="primary"):
    if not api_token:
        st.warning("請先於左側輸入 API Token！")
    elif not selected_space_key:
        st.warning("請於左側選擇有效的 Confluence Space！")
    else:
        with st.spinner("1/2 正從 Jira 撈取 Issue 資料..."):
            jira_issues = fetch_jira_issues(atlassian_url, api_email, api_token, sprint_name)
            
        if not jira_issues:
            st.warning(f"在 Jira 中找不到 Sprint 名稱為 '{sprint_name}' 的任何 Issue。")
        else:
            grouped = group_issues_by_assignee(jira_issues)
            st.success(f"成功撈取 {len(jira_issues)} 個 Issue，共涵蓋 {len(grouped)} 位團隊成員！")
            
            next_sprint_name = f"Sprint {sprint_num + 1}"
            confluence_body = build_confluence_html(grouped, department, sprint_name, next_sprint_name)
            
            with st.spinner("2/2 正在寫入至 Confluence..."):
                res = create_confluence_page(
                    domain=atlassian_url,
                    email=api_email,
                    token=api_token,
                    space_key=selected_space_key,
                    title=page_title,
                    html_content=confluence_body,
                    parent_id=selected_parent_id
                )
                
                if res.status_code == 200:
                    page_data = res.json()
                    web_link = f"{atlassian_url.rstrip('/')}/wiki{page_data.get('_links', {}).get('webui')}"
                    st.balloons()
                    st.success(f"🎉 頁面已成功發布！ [點此前往 Confluence 檢視頁面]({web_link})")
                else:
                    st.error(f"Confluence 發布失敗 ({res.status_code}): {res.text}")
