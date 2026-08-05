import streamlit as st
import requests
from requests.auth import HTTPBasicAuth
import html

st.set_page_config(page_title="WorkTicket - Jira to Confluence 工具", layout="wide", page_icon="📋")

st.title("📋 WorkTicket: Jira to Confluence 週會頁面自動生成器")

# 讀取預設設定（若 Secrets 中有設定則自動帶入，否則給預設值）
default_domain = st.secrets.get("ATLASSIAN_DOMAIN", "https://your-domain.atlassian.net")
default_email = st.secrets.get("ATLASSIAN_EMAIL", "your-email@example.com")
default_space = st.secrets.get("CONFLUENCE_SPACE", "QA")

# -----------------------------------------------------------------------------
# 側邊欄：Atlassian 連線資訊與 API 設定
# -----------------------------------------------------------------------------
with st.sidebar:
    st.header("⚙️ Atlassian API 設定")
    atlassian_url = st.text_input("Atlassian Domain", value=default_domain)
    api_email = st.text_input("API Email", value=default_email)
    api_token = st.text_input("API Token", type="password", help="請至 https://id.atlassian.com 申請 API Token")
    
    st.divider()
    confluence_space_key = st.text_input("Confluence Space Key", value=default_space)
    parent_page_id = st.text_input("父頁面 Page ID (選填)", value="")

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
# 輔助函式：Jira REST API
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# 輔助函式：建構符合 Confluence 模板的 XHTML Body
# -----------------------------------------------------------------------------
def build_confluence_html(grouped_issues, current_sprint, next_sprint):
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
# 執行按鈕與流程控制
# -----------------------------------------------------------------------------
st.header("2. 預覽與寫入")

if st.button("🚀 從 Jira 抓取資料並生成頁面", type="primary"):
    if not api_token:
        st.warning("請先於左側輸入 API Token！")
    else:
        with st.spinner("1/2 正從 Jira 撈取 Issue 資料..."):
            jira_issues = fetch_jira_issues(atlassian_url, api_email, api_token, sprint_name)
            
        if not jira_issues:
            st.warning(f"在 Jira 中找不到 Sprint 名稱為 '{sprint_name}' 的任何 Issue。")
        else:
            grouped = group_issues_by_assignee(jira_issues)
            st.success(f"成功撈取 {len(jira_issues)} 個 Issue，共涵蓋 {len(grouped)} 位團隊成員！")
            
            next_sprint_name = f"Sprint {sprint_num + 1}"
            confluence_body = build_confluence_html(grouped, sprint_name, next_sprint_name)
            
            with st.spinner("2/2 正在寫入至 Confluence..."):
                res = create_confluence_page(
                    domain=atlassian_url,
                    email=api_email,
                    token=api_token,
                    space_key=confluence_space_key,
                    title=page_title,
                    html_content=confluence_body,
                    parent_id=parent_page_id if parent_page_id else None
                )
                
                if res.status_code == 200:
                    page_data = res.json()
                    web_link = f"{atlassian_url.rstrip('/')}/wiki{page_data.get('_links', {}).get('webui')}"
                    st.balloons()
                    st.success(f"🎉 頁面已成功發布！ [點此前往 Confluence 檢視頁面]({web_link})")
                else:
                    st.error(f"Confluence 發布失敗 ({res.status_code}): {res.text}")
