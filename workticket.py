import streamlit as st
import requests
from requests.auth import HTTPBasicAuth
import html
import re
from bs4 import BeautifulSoup
import streamlit.components.v1 as components

# 匯入團隊組織設定檔
from team_config import TEAM_MEMBERS

st.set_page_config(page_title="WorkTicket - Jira to Confluence 工具", layout="wide", page_icon="📋")

st.title("📋 WorkTicket 工作週報")

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

def fetch_all_active_jira_issues(domain, email, token):
    """由 Jira JQL 撈取未完成/進行中工單 (排除 Done 狀態)"""
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

def get_member_search_aliases(department, assignee_key):
    """取得特定成員在 Confluence 比對時的所有可能名稱（別名）"""
    valid_members = TEAM_MEMBERS.get(department, [])
    aliases = [assignee_key.lower()]
    
    for m in valid_members:
        if isinstance(m, dict):
            name = m.get('name', '').lower()
            conf_name = m.get('confluence_name', '').lower()
            email = m.get('email', '').lower()
            
            # 如果匹配 Jira 名字或 Email，將其所有別名納入搜尋清單
            if assignee_key.lower() in [name, conf_name, email] or (email and assignee_key.lower() in email):
                if name: aliases.append(name)
                if conf_name: aliases.append(conf_name)
                if email: aliases.append(email.split('@')[0])
        elif isinstance(m, str):
            if assignee_key.lower() == m.lower():
                aliases.append(m.lower())
                
    return list(set(aliases))

def filter_and_group_by_dept(issues, department, sprint_num):
    """根據部門成員與 Sprint 精確欄位比對工單"""
    grouped = {}
    valid_members = TEAM_MEMBERS.get(department, [])
    
    member_keywords = []
    for m in valid_members:
        if isinstance(m, dict):
            if m.get('name'): member_keywords.append(m['name'].lower())
            if m.get('confluence_name'): member_keywords.append(m['confluence_name'].lower())
            if m.get('email'): member_keywords.append(m['email'].lower())
        elif isinstance(m, str):
            member_keywords.append(m.lower())

    sprint_str = str(sprint_num)
    debug_found_assignees = set()

    for issue in issues:
        fields = issue.get('fields', {})
        assignee_obj = fields.get('assignee')
        status_obj = fields.get('status', {})
        
        status_category_key = status_obj.get('statusCategory', {}).get('key', '')
        if status_category_key == 'done':
            continue
        
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
                
        # 精確比對 customfield_10020 (Sprint 欄位)，防止 Issue Key 誤撞數字
        sprint_field = fields.get('customfield_10020')
        has_sprint_match = False
        if sprint_field and isinstance(sprint_field, list):
            for s in sprint_field:
                s_name = s.get('name', '') if isinstance(s, dict) else str(s)
                if re.search(rf'\b{sprint_str}\b', s_name) or f"Sprint {sprint_str}" in s_name or f"sprint {sprint_str}" in s_name.lower():
                    has_sprint_match = True
                    break
        
        if is_in_dept and has_sprint_match:
            if matched_display_name not in grouped:
                grouped[matched_display_name] = []
            grouped[matched_display_name].append(issue)
            
    return grouped, debug_found_assignees

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

def build_single_issue_li_html(issue, domain):
    """使用 Confluence 原生 Smart Link 格式 (data-card-appearance="inline")"""
    key = issue.get('key')
    fields = issue.get('fields', {})
    timetracking = fields.get('timetracking', {})
    estimate_str = format_estimate(timetracking)
    
    issue_url = f"{domain.rstrip('/')}/browse/{key}"
    smart_link = f'<a href="{issue_url}" data-card-appearance="inline">{issue_url}</a>'
    return f'<li>{smart_link} {estimate_str}</li>'

def generate_user_text_for_copy(issues, domain):
    """僅生成工單連結與估時的純文字（供一鍵複製）"""
    lines = []
    for issue in issues:
        key = issue.get('key')
        fields = issue.get('fields', {})
        estimate_str = format_estimate(fields.get('timetracking', {}))
        jira_url = f"{domain.rstrip('/')}/browse/{key}"
        lines.append(f"{jira_url} {estimate_str}")
    
    return "\n".join(lines)

def render_copy_button(button_label, text_to_copy, button_id):
    """JavaScript 一鍵複製實體按鈕"""
    escaped_text = text_to_copy.replace('\\', '\\\\').replace('`', '\\`').replace('$', '\\$').replace('\n', '\\n')
    html_code = f"""
    <button id="btn_{button_id}" style="
        background-color: #4CAF50;
        color: white;
        padding: 6px 14px;
        border: none;
        border-radius: 4px;
        cursor: pointer;
        font-size: 14px;
        font-weight: 500;
        margin-top: 4px;
    ">{button_label}</button>

    <script>
    document.getElementById("btn_{button_id}").onclick = function() {{
        const text = "{escaped_text}";
        navigator.clipboard.writeText(text).then(() => {{
            const btn = document.getElementById("btn_{button_id}");
            btn.innerText = "✓ 已複製！";
            btn.style.backgroundColor = "#2E7D32";
            setTimeout(() => {{
                btn.innerText = "{button_label}";
                btn.style.backgroundColor = "#4CAF50";
            }}, 2000);
        }}).catch(err => {{
            console.error('複製失敗:', err);
        }});
    }};
    </script>
    """
    components.html(html_code, height=45)

def update_confluence_page_by_user(domain, email, token, space_key, title, grouped_issues, selected_assignees, current_sprint_num, department, parent_id=None):
    """局部更新或補充工單至 Confluence（支援多名稱/別名精確比對與精確 Sprint 標題定位）"""
    auth = HTTPBasicAuth(email, token)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    
    search_url = f"{domain.rstrip('/')}/wiki/rest/api/content"
    params = {
        "spaceKey": space_key,
        "title": title,
        "expand": "body.storage,version,ancestors"
    }
    
    res = requests.get(search_url, params=params, auth=auth)
    if res.status_code != 200 or not res.json().get('results'):
        return False, f"找不到名稱為『{title}』的頁面，請確認標題與 Space/層級 選擇是否正確。"
        
    results = res.json()['results']
    target_page = results[0]
    
    if parent_id:
        for p in results:
            ancestor_ids = [str(anc.get('id')) for anc in p.get('ancestors', [])]
            if str(parent_id) in ancestor_ids:
                target_page = p
                break
                
    page_id = target_page['id']
    current_version = target_page['version']['number']
    original_html = target_page['body']['storage']['value']
    
    soup = BeautifulSoup(original_html, 'html.parser')
    updated_count = 0
    added_issues_count = 0
    
    sprint_header_text = f"AP Sprint {current_sprint_num}"

    rows = soup.find_all('tr')
    for row in rows:
        cols = row.find_all('td')
        if len(cols) >= 2:
            first_col_text = cols[0].get_text().strip().lower()
            
            for assignee in selected_assignees:
                aliases = get_member_search_aliases(department, assignee)
                
                if any(alias in first_col_text for alias in aliases):
                    updated_count += 1
                    target_td = cols[1]
                    
                    td_raw_str = str(target_td)
                    existing_keys = set(re.findall(r'[A-Z0-9]+-\d+', td_raw_str))
                    
                    latest_user_issues = grouped_issues.get(assignee, [])
                    new_issues_to_add = [i for i in latest_user_issues if i.get('key') not in existing_keys]
                    
                    if new_issues_to_add:
                        target_ul = None
                        
                        # 1. 優先尋找 AP Sprint {current_sprint_num} 標題後緊跟著的 <ul>
                        sprint_elements = target_td.find_all(text=re.compile(sprint_header_text, re.IGNORECASE))
                        for elem in sprint_elements:
                            parent_tag = elem.parent
                            sibling_ul = parent_tag.find_next_sibling('ul')
                            if sibling_ul:
                                target_ul = sibling_ul
                                break
                        
                        # 2. 如果指定 Sprint 底下沒有 <ul>，則在該標題後方即時插入一個空的 <ul>
                        if not target_ul and sprint_elements:
                            target_ul = soup.new_tag('ul')
                            sprint_elements[0].parent.insert_after(target_ul)
                        
                        # 3. 若找不到任何相關標題，才降級為第一個 <ul> 或直接 append
                        if not target_ul:
                            target_ul = target_td.find('ul')
                            if not target_ul:
                                target_ul = soup.new_tag('ul')
                                target_td.append(target_ul)

                        # 插入新工單
                        for new_issue in new_issues_to_add:
                            li_html = build_single_issue_li_html(new_issue, domain)
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
            
            root_pages = fetch_child_pages(atlassian_url, api_email, api_token, space_key=selected_space_key)
            if root_pages:
                l1_options = ["(無，存放在 Space 根目錄)"] + list(root_pages.keys())
                idx_l1 = l1_options.index("QA Home") if "QA Home" in l1_options else 0
                selected_l1 = st.selectbox("2. 選擇頂層父頁面 (L1)", l1_options, index=idx_l1)
                
                if selected_l1 != "(無，存放在 Space 根目錄)":
                    selected_parent_id = root_pages[selected_l1]
                    
                    l2_pages = fetch_child_pages(atlassian_url, api_email, api_token, parent_id=selected_parent_id)
                    if l2_pages:
                        l2_options = ["(停在此層，做為父頁面)"] + list(l2_pages.keys())
                        idx_l2 = l2_options.index("QA weekly meeting") if "QA weekly meeting" in l2_options else 0
                        selected_l2 = st.selectbox("3. 選擇次層子頁面 (L2)", l2_options, index=idx_l2)
                        
                        if selected_l2 != "(停在此層，做為父頁面)":
                            selected_parent_id = l2_pages[selected_l2]
                            
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
# 主要區域
# -----------------------------------------------------------------------------
st.header("1. 選擇生成與更新條件")
col1, col2 = st.columns(2)

with col1:
    department = st.selectbox("部門", list(TEAM_MEMBERS.keys()))
with col2:
    sprint_num = st.number_input("Sprint 號碼", min_value=1, max_value=200, value=53)

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
                    st.success(f"🎉 成功抓取！已準備好 {len(grouped_dept_issues)} 位同仁的未完成工單。")
                else:
                    st.warning(f"已抓取到工單，但未比對到 Sprint {sprint_num} 的資料。")
            else:
                st.warning("無法取得工單。")

if st.session_state.grouped_issues:
    dept_assignees = sorted(list(st.session_state.grouped_issues.keys()))
    
    st.divider()
    st.subheader("👥 批次更新操作區")
    
    selected_assignees = st.multiselect("勾選要批次更新的成員：", options=dept_assignees, default=dept_assignees)
    
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
                    current_sprint_num=sprint_num,
                    department=department,
                    parent_id=selected_parent_id
                )
                if success:
                    st.balloons()
                    st.success(msg)
                else:
                    st.error(msg)

    st.divider()
    st.subheader("👤 個人排版預覽與單獨更新 / 複製區")

    for idx, assignee in enumerate(dept_assignees):
        issues = st.session_state.grouped_issues.get(assignee, [])
        copy_text = generate_user_text_for_copy(issues, atlassian_url)
        
        with st.expander(f"👤 {assignee} ({len(issues)} 筆進行中工單)", expanded=True):
            col_preview, col_action = st.columns([3, 1.2])
            
            with col_preview:
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
            
            with col_action:
                st.write("**單獨操作選項**")
                
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
                                current_sprint_num=sprint_num,
                                department=department,
                                parent_id=selected_parent_id
                            )
                            if success:
                                st.success(msg)
                            else:
                                st.error(msg)
                
                # 2. 僅複製工單 URL 與估時
                render_copy_button(f"📋 複製 {assignee} 工單連結", copy_text, f"copy_{idx}")
