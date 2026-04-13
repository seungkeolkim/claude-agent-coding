/* ═══════════════════════════════════════════════════════════
   Agent Hub Console — SPA 메인 로직
   ═══════════════════════════════════════════════════════════ */

// ─── API 헬퍼 ───

async function api(url, options = {}) {
    const resp = await fetch(url, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
    });
    return resp.json();
}

async function dispatch(action, project, params = {}) {
    return api('/api/dispatch', {
        method: 'POST',
        body: JSON.stringify({ action, project, params, source: 'web' }),
    });
}

// ─── 탭 전환 ───

document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');

        // 탭 전환 시 데이터 로드
        const tab = btn.dataset.tab;
        if (tab === 'dashboard') loadDashboard();
        else if (tab === 'tasks') loadTasks();
        else if (tab === 'notifications') loadNotifications();
    });
});

// ─── 시간 포맷 ───

function formatTime(iso) {
    if (!iso) return '-';
    const d = new Date(iso);
    return d.toLocaleString('ko-KR', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function statusBadge(status) {
    return `<span class="task-status status-${status}">${status}</span>`;
}

// ═══════════════════════════════════════════════════════════
// Dashboard
// ═══════════════════════════════════════════════════════════

async function loadDashboard() {
    // 시스템 상태
    const statusResp = await api('/api/status');
    const statusEl = document.getElementById('system-status');
    if (statusResp.success) {
        const s = statusResp.data;
        const tmStatus = s.tm_running ? 'running' : 'stopped';
        const tmIndicator = document.getElementById('tm-status');
        tmIndicator.className = `status-indicator ${tmStatus}`;
        tmIndicator.title = `Task Manager: ${tmStatus}`;
        statusEl.innerHTML = `
            <div class="project-card">
                <div>Task Manager: ${statusBadge(tmStatus)}
                ${s.tm_pid ? ` (PID: ${s.tm_pid})` : ''}</div>
            </div>`;
    }

    // 프로젝트 카드
    const projResp = await api('/api/projects');
    const cardsEl = document.getElementById('project-cards');
    if (projResp.success && projResp.data.length > 0) {
        cardsEl.innerHTML = projResp.data.map(p => {
            const counts = p.task_counts || {};
            const total = Object.values(counts).reduce((a, b) => a + b, 0);
            const active = (counts.submitted || 0) + (counts.queued || 0) +
                           (counts.in_progress || 0) + (counts.waiting_for_human_plan_confirm || 0);
            const wfcInfo = p.wfc_pid
                ? `<div class="counts" style="opacity:0.7">WFC PID: ${p.wfc_pid}</div>`
                : '';
            return `
                <div class="project-card">
                    <div class="name">${p.name}</div>
                    <div class="status status-${p.status}">${p.status}</div>
                    ${p.current_task_id ? `<div class="counts">Current: #${p.current_task_id}</div>` : ''}
                    ${wfcInfo}
                    <div class="counts">${active} active / ${total} total</div>
                    ${p.unread_notifications > 0 ? `<div class="counts" style="color:var(--warning)">Unread: ${p.unread_notifications}</div>` : ''}
                </div>`;
        }).join('');
    } else {
        cardsEl.innerHTML = '<div class="empty">No projects</div>';
    }

    // 승인 대기
    const pendingResp = await api('/api/pending');
    const pendingEl = document.getElementById('pending-list');
    if (pendingResp.success && pendingResp.data.length > 0) {
        pendingEl.innerHTML = pendingResp.data.map(item => `
            <div class="pending-item">
                <div class="pending-header">
                    <span>${item.project} #${item.task_id}</span>
                    <span class="pending-type">${item.interaction_type}</span>
                </div>
                <div>${item.message}</div>
                <div class="pending-actions">
                    ${item.interaction_type === 'waiting_for_human_pr_approve' ? `
                        <button class="btn btn-success" onclick="handleMergePr('${item.project}', '${item.task_id}')">Merge PR Now</button>
                        <button class="btn btn-danger" onclick="handleClosePr('${item.project}', '${item.task_id}')">Close PR Now</button>
                        <button class="btn btn-outline-success" onclick="handleCompletePrReview('${item.project}', '${item.task_id}', 'merged')">Mark as Merged</button>
                        <button class="btn btn-outline-danger" onclick="handleCompletePrReview('${item.project}', '${item.task_id}', 'rejected')">Mark as Rejected</button>
                    ` : `
                        <button class="btn btn-success" onclick="handleApprove('${item.project}', '${item.task_id}')">Approve</button>
                        <button class="btn btn-danger" onclick="handleReject('${item.project}', '${item.task_id}')">Reject</button>
                        <button class="btn btn-secondary" onclick="viewPlan('${item.project}', '${item.task_id}')">View Plan</button>
                        <button class="btn btn-warning" onclick="handleCancel('${item.project}', '${item.task_id}')">Cancel</button>
                    `}
                </div>
            </div>
        `).join('');
    } else {
        pendingEl.innerHTML = '<div class="empty">No pending approvals</div>';
    }

    // 알림 배지
    const notiResp = await api('/api/notifications?unread_only=true&limit=1');
    const badge = document.getElementById('notification-badge');
    if (notiResp.unread_count > 0) {
        badge.textContent = notiResp.unread_count;
        badge.classList.remove('hidden');
    } else {
        badge.classList.add('hidden');
    }
}

// ═══════════════════════════════════════════════════════════
// Tasks
// ═══════════════════════════════════════════════════════════

async function loadTasks() {
    const project = document.getElementById('filter-project').value;
    const status = document.getElementById('filter-status').value;
    let url = '/api/tasks?';
    if (project) url += `project=${project}&`;
    if (status) url += `status=${status}&`;

    const resp = await api(url);
    const listEl = document.getElementById('task-list');

    if (resp.success && resp.data.length > 0) {
        listEl.innerHTML = resp.data.map(t => `
            <div class="task-row" data-project="${t.project}" data-task-id="${t.task_id}"
                 onclick="toggleTaskDetail('${t.project}', '${t.task_id}', this)">
                <span class="task-id">#${t.task_id}</span>
                <span class="task-title">${t.title}</span>
                ${statusBadge(t.status)}
                ${t.pipeline_stage && t.status === 'in_progress' ? `<span class="pipeline-stage">${t.pipeline_stage}${t.pipeline_stage_detail ? ' (' + t.pipeline_stage_detail + ')' : ''}</span>` : ''}
                ${t.failure_reason ? `<span class="failure-reason" title="${t.failure_reason}">!</span>` : ''}
                <span class="task-time">${formatTime(t.submitted_at)}</span>
            </div>
        `).join('');
    } else {
        listEl.innerHTML = '<div class="empty">No tasks</div>';
    }

    // 프로젝트 필터 옵션 채우기
    const projResp = await api('/api/projects');
    const select = document.getElementById('filter-project');
    const currentVal = select.value;
    select.innerHTML = '<option value="">All Projects</option>';
    if (projResp.success) {
        projResp.data.forEach(p => {
            select.innerHTML += `<option value="${p.name}" ${p.name === currentVal ? 'selected' : ''}>${p.name}</option>`;
        });
    }
}

async function toggleTaskDetail(project, taskId, rowEl) {
    // 이미 열린 detail이 있으면 닫기
    const existing = rowEl.nextElementSibling;
    if (existing && existing.classList.contains('task-detail-inline')) {
        existing.remove();
        rowEl.classList.remove('expanded');
        return;
    }

    // 다른 열린 detail 모두 닫기
    document.querySelectorAll('.task-detail-inline').forEach(el => {
        el.previousElementSibling?.classList.remove('expanded');
        el.remove();
    });

    const resp = await api(`/api/tasks/${project}/${taskId}`);
    if (!resp.success) return;

    const t = resp.data;
    rowEl.classList.add('expanded');

    const detailDiv = document.createElement('div');
    detailDiv.className = 'task-detail-inline';
    detailDiv.innerHTML = `
        <div class="detail-field"><label>Status:</label> ${statusBadge(t.status)}</div>
        <div class="detail-field"><label>Submitted:</label> ${formatTime(t.submitted_at)} via ${t.submitted_via || '-'}</div>
        <div class="detail-field"><label>Branch:</label> ${t.branch || '-'}</div>
        <div class="detail-field"><label>PR:</label> ${t.pr_url ? `<a href="${t.pr_url}" target="_blank" style="color:var(--accent)">${t.pr_url}</a>` : '-'}</div>
        <div class="detail-field"><label>Summary:</label> ${t.summary || '-'}</div>
        ${t.pipeline_stage ? `<div class="detail-field"><label>Pipeline Stage:</label> <span class="pipeline-stage">${t.pipeline_stage}</span>${t.pipeline_stage_detail ? ` (${t.pipeline_stage_detail})` : ''}${t.pipeline_stage_updated_at ? ` — ${formatTime(t.pipeline_stage_updated_at)}` : ''}</div>` : ''}
        ${t.failure_reason ? `<div class="detail-field failure-box"><label>Failure Reason:</label> ${t.failure_reason}</div>` : ''}
        ${t.description ? `<div class="detail-field"><label>Description:</label><br>${t.description}</div>` : ''}
        <div class="detail-actions">
            ${t.status === 'waiting_for_human_plan_confirm' ? `
                <button class="btn btn-success" onclick="handleApprove('${project}', '${taskId}')">Approve</button>
                <button class="btn btn-danger" onclick="handleReject('${project}', '${taskId}')">Reject</button>
            ` : ''}
            ${t.status === 'waiting_for_human_pr_approve' ? `
                <button class="btn btn-success" onclick="handleMergePr('${project}', '${taskId}')">Merge PR Now</button>
                <button class="btn btn-danger" onclick="handleClosePr('${project}', '${taskId}')">Close PR Now</button>
                <button class="btn btn-secondary" onclick="handleCompletePrReview('${project}', '${taskId}', 'merged')">Mark as Merged</button>
                <button class="btn btn-secondary" onclick="handleCompletePrReview('${project}', '${taskId}', 'rejected')">Mark as Rejected</button>
            ` : ''}
            ${['submitted', 'queued', 'planned', 'in_progress', 'waiting_for_human_plan_confirm'].includes(t.status) ? `
                <button class="btn btn-warning" onclick="handleCancel('${project}', '${taskId}')">Cancel</button>
            ` : ''}
            ${['cancelled', 'failed'].includes(t.status) ? `
                <button class="btn btn-primary" onclick="handleResubmit('${project}', '${taskId}')">Resubmit</button>
            ` : ''}
            ${['in_progress'].includes(t.status) ? `
                <button class="btn btn-secondary" onclick="handleFeedback('${project}', '${taskId}')">Feedback</button>
            ` : ''}
            <button class="btn btn-secondary" onclick="viewPlan('${project}', '${taskId}')">View Plan</button>
        </div>
    `;

    rowEl.after(detailDiv);
}

// ═══════════════════════════════════════════════════════════
// Notifications
// ═══════════════════════════════════════════════════════════

async function loadNotifications() {
    const resp = await api('/api/notifications?limit=100');
    const listEl = document.getElementById('notification-list');
    if (resp.success && resp.data.length > 0) {
        listEl.innerHTML = resp.data.map(n => `
            <div class="notification-item ${n.read ? '' : 'unread'}">
                <div class="noti-header">
                    <span class="noti-type">${n.event_type} (${n.project}${n.task_id ? ` #${n.task_id}` : ''})</span>
                    <span class="noti-time">${formatTime(n.created_at)}</span>
                </div>
                <div class="noti-message">${n.message}</div>
            </div>
        `).join('');
    } else {
        listEl.innerHTML = '<div class="empty">No notifications</div>';
    }
}

// ═══════════════════════════════════════════════════════════
// Actions (모달 기반)
// ═══════════════════════════════════════════════════════════

function showModal(title, bodyHtml, buttons) {
    document.getElementById('modal-title').textContent = title;
    document.getElementById('modal-body').innerHTML = bodyHtml;
    document.getElementById('modal-footer').innerHTML = buttons.map(b =>
        `<button class="btn ${b.cls}" onclick="${b.onclick}">${b.label}</button>`
    ).join('');
    document.getElementById('modal-overlay').classList.remove('hidden');
}

function closeModal() {
    document.getElementById('modal-overlay').classList.add('hidden');
}

document.getElementById('modal-close').addEventListener('click', closeModal);
document.getElementById('modal-overlay').addEventListener('click', e => {
    if (e.target === document.getElementById('modal-overlay')) closeModal();
});

async function handleApprove(project, taskId) {
    showModal('Approve', `<p>${project} #${taskId} plan을 승인합니다.</p>
        <label>Message (optional):</label>
        <input id="approve-msg" type="text" />`, [
        { label: 'Cancel', cls: 'btn-secondary', onclick: 'closeModal()' },
        { label: 'Approve', cls: 'btn-success', onclick: `doApprove('${project}','${taskId}')` },
    ]);
}

async function doApprove(project, taskId) {
    const msg = document.getElementById('approve-msg').value;
    await dispatch('approve', project, { task_id: taskId, message: msg || undefined });
    closeModal();
    loadDashboard();
    loadTasks();
}

async function handleReject(project, taskId) {
    showModal('Reject', `<p>${project} #${taskId}에 수정을 요청합니다.</p>
        <label>Reason (required):</label>
        <textarea id="reject-msg"></textarea>`, [
        { label: 'Cancel', cls: 'btn-secondary', onclick: 'closeModal()' },
        { label: 'Reject', cls: 'btn-danger', onclick: `doReject('${project}','${taskId}')` },
    ]);
}

async function doReject(project, taskId) {
    const msg = document.getElementById('reject-msg').value;
    if (!msg.trim()) { alert('사유를 입력해주세요.'); return; }
    await dispatch('reject', project, { task_id: taskId, message: msg });
    closeModal();
    loadDashboard();
    loadTasks();
}

async function handleCompletePrReview(project, taskId, result) {
    const label = result === 'merged' ? 'Mark as Merged' : 'Mark as Rejected';
    const msgLabel = result === 'merged' ? 'Message (optional):' : 'Reason (optional):';
    showModal(label, `<p>${project} #${taskId} PR 상태를 수동 반영합니다.</p>
        <label>${msgLabel}</label>
        <input id="pr-review-msg" type="text" />`, [
        { label: 'Cancel', cls: 'btn-secondary', onclick: 'closeModal()' },
        { label: label, cls: result === 'merged' ? 'btn-outline-success' : 'btn-outline-danger',
          onclick: `doCompletePrReview('${project}','${taskId}','${result}')` },
    ]);
}

async function doCompletePrReview(project, taskId, result) {
    const msg = document.getElementById('pr-review-msg').value;
    await dispatch('complete_pr_review', project, { task_id: taskId, result: result, message: msg || undefined });
    closeModal();
    loadDashboard();
    loadTasks();
}

async function handleMergePr(project, taskId) {
    showModal('Merge PR Now', `<p>${project} #${taskId} PR을 직접 머지합니다.</p>
        <label>Message (optional):</label>
        <input id="merge-pr-msg" type="text" />`, [
        { label: 'Cancel', cls: 'btn-secondary', onclick: 'closeModal()' },
        { label: 'Merge PR Now', cls: 'btn-success',
          onclick: `doMergePr('${project}','${taskId}')` },
    ]);
}

async function doMergePr(project, taskId) {
    const msg = document.getElementById('merge-pr-msg').value;
    await dispatch('merge_pr', project, { task_id: taskId, message: msg || undefined });
    closeModal();
    // 즉시 UI를 Processing 상태로 전환
    setPrProcessing(project, taskId);
}

async function handleClosePr(project, taskId) {
    showModal('Close PR Now', `<p>${project} #${taskId} PR을 직접 닫습니다.</p>
        <label>Reason (optional):</label>
        <input id="close-pr-msg" type="text" />`, [
        { label: 'Cancel', cls: 'btn-secondary', onclick: 'closeModal()' },
        { label: 'Close PR Now', cls: 'btn-danger',
          onclick: `doClosePr('${project}','${taskId}')` },
    ]);
}

async function doClosePr(project, taskId) {
    const msg = document.getElementById('close-pr-msg').value;
    await dispatch('close_pr', project, { task_id: taskId, message: msg || undefined });
    closeModal();
    // 즉시 UI를 Processing 상태로 전환
    setPrProcessing(project, taskId);
}

// ─── PR 비동기 처리 UI ───

function setPrProcessing(project, taskId) {
    // pending-list 카드의 버튼 영역을 Processing 메시지로 교체
    document.querySelectorAll('.pending-item').forEach(item => {
        const header = item.querySelector('.pending-header span');
        if (header && header.textContent.trim() === `${project} #${taskId}`) {
            const actions = item.querySelector('.pending-actions');
            if (actions) {
                actions.innerHTML = '<div class="pr-processing">Processing PR...</div>';
            }
        }
    });
    // task detail 영역의 버튼도 교체
    document.querySelectorAll('.task-detail-inline').forEach(detail => {
        const row = detail.previousElementSibling;
        if (row && row.dataset.project === project && row.dataset.taskId === taskId) {
            const actions = detail.querySelector('.detail-actions');
            if (actions) {
                actions.innerHTML = '<div class="pr-processing">Processing PR...</div>';
            }
        }
    });
}

function showPrError(project, taskId, errorMsg) {
    // pending-list 카드에 버튼 복원 + 에러 메시지 표시
    document.querySelectorAll('.pending-item').forEach(item => {
        const header = item.querySelector('.pending-header span');
        if (header && header.textContent.trim() === `${project} #${taskId}`) {
            const actions = item.querySelector('.pending-actions');
            if (actions) {
                actions.innerHTML = `
                    <button class="btn btn-success" onclick="handleMergePr('${project}', '${taskId}')">Merge PR Now</button>
                    <button class="btn btn-danger" onclick="handleClosePr('${project}', '${taskId}')">Close PR Now</button>
                    <button class="btn btn-outline-success" onclick="handleCompletePrReview('${project}', '${taskId}', 'merged')">Mark as Merged</button>
                    <button class="btn btn-outline-danger" onclick="handleCompletePrReview('${project}', '${taskId}', 'rejected')">Mark as Rejected</button>
                    <div class="pr-error">${errorMsg}</div>
                `;
            }
        }
    });
    // task detail 영역도 복원
    document.querySelectorAll('.task-detail-inline').forEach(detail => {
        const row = detail.previousElementSibling;
        if (row && row.dataset.project === project && row.dataset.taskId === taskId) {
            const actions = detail.querySelector('.detail-actions');
            if (actions) {
                actions.innerHTML = `
                    <button class="btn btn-success" onclick="handleMergePr('${project}', '${taskId}')">Merge PR Now</button>
                    <button class="btn btn-danger" onclick="handleClosePr('${project}', '${taskId}')">Close PR Now</button>
                    <button class="btn btn-secondary" onclick="handleCompletePrReview('${project}', '${taskId}', 'merged')">Mark as Merged</button>
                    <button class="btn btn-secondary" onclick="handleCompletePrReview('${project}', '${taskId}', 'rejected')">Mark as Rejected</button>
                    <button class="btn btn-warning" onclick="handleCancel('${project}', '${taskId}')">Cancel</button>
                    <div class="pr-error">${errorMsg}</div>
                `;
            }
        }
    });
}

async function handleCancel(project, taskId) {
    if (!confirm(`${project} #${taskId}를 취소합니까?`)) return;
    await dispatch('cancel', project, { task_id: taskId });
    loadTasks();
}

async function handleResubmit(project, taskId) {
    if (!confirm(`${project} #${taskId}를 재제출합니까?`)) return;
    await dispatch('resubmit', project, { task_id: taskId });
    loadTasks();
}

async function handleFeedback(project, taskId) {
    showModal('Feedback', `<p>${project} #${taskId}에 피드백을 전송합니다.</p>
        <label>Message:</label>
        <textarea id="feedback-msg"></textarea>`, [
        { label: 'Cancel', cls: 'btn-secondary', onclick: 'closeModal()' },
        { label: 'Send', cls: 'btn-primary', onclick: `doFeedback('${project}','${taskId}')` },
    ]);
}

async function doFeedback(project, taskId) {
    const msg = document.getElementById('feedback-msg').value;
    if (!msg.trim()) { alert('메시지를 입력해주세요.'); return; }
    await dispatch('feedback', project, { task_id: taskId, message: msg });
    closeModal();
}

async function viewPlan(project, taskId) {
    const resp = await api(`/api/tasks/${project}/${taskId}/plan`);
    if (resp.success) {
        showModal(`Plan — ${project} #${taskId}`,
            `<pre>${escapeHtml(JSON.stringify(resp.data, null, 2))}</pre>`, [
            { label: 'Close', cls: 'btn-secondary', onclick: 'closeModal()' },
        ]);
    } else {
        showModal('Plan', `<p>${resp.message || 'Plan이 아직 없습니다.'}</p>`, [
            { label: 'Close', cls: 'btn-secondary', onclick: 'closeModal()' },
        ]);
    }
}

// ─── New Task ───

document.getElementById('btn-new-task').addEventListener('click', () => {
    const projOptions = Array.from(document.getElementById('filter-project').options)
        .filter(o => o.value)
        .map(o => `<option value="${o.value}">${o.value}</option>`).join('');

    showModal('New Task', `
        <label>Project:</label>
        <select id="new-task-project">${projOptions}</select>
        <label>Title:</label>
        <input id="new-task-title" type="text" />
        <label>Description:</label>
        <textarea id="new-task-desc"></textarea>
        <label>Priority:</label>
        <select id="new-task-priority">
            <option value="default" selected>default (보통)</option>
            <option value="urgent">urgent (급함)</option>
            <option value="critical">critical (긴급)</option>
        </select>
    `, [
        { label: 'Cancel', cls: 'btn-secondary', onclick: 'closeModal()' },
        { label: 'Submit', cls: 'btn-primary', onclick: 'doSubmitTask()' },
    ]);
});

async function doSubmitTask() {
    const project = document.getElementById('new-task-project').value;
    const title = document.getElementById('new-task-title').value;
    const description = document.getElementById('new-task-desc').value;
    const priority = document.getElementById('new-task-priority').value;
    if (!project || !title.trim()) { alert('프로젝트와 제목을 입력해주세요.'); return; }
    await dispatch('submit', project, { title, description, priority });
    closeModal();
    loadTasks();
}

// ─── 필터 이벤트 ───

document.getElementById('filter-project').addEventListener('change', loadTasks);
document.getElementById('filter-status').addEventListener('change', loadTasks);

// ═══════════════════════════════════════════════════════════
// Chat — 실시간 양방향 채팅 + 세션 사이드바
// ═══════════════════════════════════════════════════════════

// localStorage → sessionStorage 마이그레이션 (1회)
if (!sessionStorage.getItem('chat_session_id') && localStorage.getItem('chat_session_id')) {
    sessionStorage.setItem('chat_session_id', localStorage.getItem('chat_session_id'));
    localStorage.removeItem('chat_session_id');
}
let chatSessionId = sessionStorage.getItem('chat_session_id');

document.getElementById('btn-chat-send').addEventListener('click', sendChat);
document.getElementById('chat-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChat();
    }
});
// textarea 높이 자동 조절
document.getElementById('chat-input').addEventListener('input', e => {
    e.target.style.height = 'auto';
    e.target.style.height = e.target.scrollHeight + 'px';
});
document.getElementById('btn-new-chat-session').addEventListener('click', () => {
    newChatSession();
    closeChatSidebar();
});
document.getElementById('btn-chat-sidebar').addEventListener('click', toggleChatSidebar);
document.getElementById('chat-sidebar-overlay').addEventListener('click', closeChatSidebar);

// ─── 사이드바 ───

function toggleChatSidebar() {
    const sidebar = document.getElementById('chat-sidebar');
    const overlay = document.getElementById('chat-sidebar-overlay');
    if (sidebar.classList.contains('open')) {
        closeChatSidebar();
    } else {
        sidebar.classList.add('open');
        overlay.classList.add('open');
        loadChatSessionList();
    }
}

function closeChatSidebar() {
    document.getElementById('chat-sidebar').classList.remove('open');
    document.getElementById('chat-sidebar-overlay').classList.remove('open');
}

async function loadChatSessionList() {
    const resp = await api('/api/chat/sessions');
    if (!resp.success) return;

    const listEl = document.getElementById('chat-session-list');
    // updated_at 기준 정렬 (서버는 session_id 순)
    const sorted = resp.data.sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''));

    if (sorted.length === 0) {
        listEl.innerHTML = '<div class="empty" style="padding:20px;font-size:0.85rem;">세션이 없습니다</div>';
        return;
    }

    listEl.innerHTML = sorted.map(s => {
        const isActive = s.session_id === chatSessionId ? ' active' : '';
        const title = s.title || s.first_message || _sessionDisplayName(s.session_id);
        const meta = _formatSessionMeta(s);
        return `<div class="chat-session-item${isActive}" data-sid="${s.session_id}">
            <div class="session-info" onclick="switchChatSession('${s.session_id}')">
                <div class="session-title">${escapeHtml(title)}</div>
                <div class="session-meta">${escapeHtml(meta)}</div>
            </div>
            <div class="session-actions">
                <button class="session-action-btn" onclick="startRenameSession(event, '${s.session_id}')" title="이름 변경">&#9998;</button>
                <button class="session-action-btn delete" onclick="deleteChatSession(event, '${s.session_id}')" title="삭제">&#10005;</button>
            </div>
        </div>`;
    }).join('');
}

function _sessionDisplayName(sessionId) {
    const m = sessionId.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})/);
    if (m) return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
    return sessionId;
}

function _formatSessionMeta(s) {
    const parts = [`${s.turn_count || 0}턴`];
    if (s.updated_at) {
        const diffMin = Math.floor((Date.now() - new Date(s.updated_at).getTime()) / 60000);
        if (diffMin < 1) parts.push('방금 전');
        else if (diffMin < 60) parts.push(`${diffMin}분 전`);
        else if (diffMin < 1440) parts.push(`${Math.floor(diffMin / 60)}시간 전`);
        else parts.push(`${Math.floor(diffMin / 1440)}일 전`);
    }
    return parts.join(' · ');
}

// ─── 세션 전환/rename/delete ───

async function switchChatSession(sessionId) {
    chatSessionId = sessionId;
    sessionStorage.setItem('chat_session_id', sessionId);
    await initChatSession();
    closeChatSidebar();
}

function startRenameSession(event, sessionId) {
    event.stopPropagation();
    const item = event.target.closest('.chat-session-item');
    const titleEl = item.querySelector('.session-title');
    const currentTitle = titleEl.textContent;

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'session-rename-input';
    input.value = currentTitle;
    titleEl.replaceWith(input);
    input.focus();
    input.select();

    function finishRename() {
        const newTitle = input.value.trim();
        if (newTitle && newTitle !== currentTitle) {
            api(`/api/chat/sessions/${sessionId}`, {
                method: 'PATCH',
                body: JSON.stringify({ title: newTitle }),
            }).then(() => {
                loadChatSessionList();
                if (sessionId === chatSessionId) {
                    document.getElementById('chat-session-title').textContent = newTitle;
                }
            });
        } else {
            loadChatSessionList();
        }
    }

    input.addEventListener('blur', finishRename);
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter') input.blur();
        if (e.key === 'Escape') { input.value = currentTitle; input.blur(); }
    });
}

async function deleteChatSession(event, sessionId) {
    event.stopPropagation();
    if (!confirm('이 대화를 삭제하시겠습니까?')) return;

    await api(`/api/chat/sessions/${sessionId}`, { method: 'DELETE' });

    if (sessionId === chatSessionId) {
        sessionStorage.removeItem('chat_session_id');
        chatSessionId = null;
        await initChatSession();
    }
    loadChatSessionList();
}

// ─── 세션 초기화/생성 ───

async function initChatSession() {
    const body = chatSessionId ? { session_id: chatSessionId } : {};
    const resp = await api('/api/chat/session', {
        method: 'POST',
        body: JSON.stringify(body),
    });
    if (resp.success) {
        chatSessionId = resp.session_id;
        sessionStorage.setItem('chat_session_id', chatSessionId);

        // 헤더 제목 갱신
        updateChatTitle();

        // 기존 히스토리 렌더링
        const messagesEl = document.getElementById('chat-messages');
        messagesEl.innerHTML = '';
        if (resp.history && resp.history.length > 0) {
            resp.history.forEach(entry => {
                appendChatMsg(entry.role, entry.content);
            });
        }
    }
}

async function updateChatTitle() {
    const titleEl = document.getElementById('chat-session-title');
    const resp = await api('/api/chat/sessions');
    if (!resp.success) { titleEl.textContent = 'New Chat'; return; }
    const s = resp.data.find(x => x.session_id === chatSessionId);
    if (s) {
        titleEl.textContent = s.title || s.first_message || _sessionDisplayName(chatSessionId);
    } else {
        titleEl.textContent = chatSessionId ? _sessionDisplayName(chatSessionId) : 'New Chat';
    }
}

async function newChatSession() {
    sessionStorage.removeItem('chat_session_id');
    chatSessionId = null;
    await initChatSession();
}

async function sendChat() {
    const input = document.getElementById('chat-input');
    const msg = input.value.trim();
    if (!msg) return;

    if (!chatSessionId) {
        await initChatSession();
    }

    appendChatMsg('user', msg);
    input.value = '';
    input.style.height = 'auto';

    // fire-and-forget — 응답은 SSE로 수신
    api('/api/chat/send', {
        method: 'POST',
        body: JSON.stringify({ session_id: chatSessionId, message: msg }),
    });
}

function _formatNotificationForChat(data) {
    const typeLabels = {
        'task_completed': '✅ Task 완료',
        'task_failed': '❌ Task 실패',
        'pr_created': '🔗 PR 생성됨',
        'plan_review_requested': '📋 Plan 승인 요청',
        'replan_review_requested': '📋 Re-plan 승인 요청',
        'escalation': '🚨 에스컬레이션',
    };
    const eventType = data.event_type || data.type || '';
    const label = typeLabels[eventType] || eventType;
    const project = data.project || '';
    const taskId = data.task_id || '';
    const message = data.message || '';

    const parts = [label];
    if (project && taskId) parts.push(`${project} #${taskId}`);
    else if (project) parts.push(project);
    if (message) parts.push(message);

    return parts.join(' — ');
}

function appendChatMsg(role, text) {
    // typing indicator가 있으면 그 앞에 삽입
    const messagesEl = document.getElementById('chat-messages');
    const typingEl = messagesEl.querySelector('.chat-typing');

    const el = document.createElement('div');
    el.className = `chat-msg ${role}`;
    // 공백/줄바꿈 보존은 CSS white-space: pre-wrap에 위임. textContent로 XSS 방지.
    el.textContent = text;

    if (typingEl) {
        messagesEl.insertBefore(el, typingEl);
    } else {
        messagesEl.appendChild(el);
    }
    el.scrollIntoView({ behavior: 'smooth' });
}

function showTypingIndicator() {
    const messagesEl = document.getElementById('chat-messages');
    if (messagesEl.querySelector('.chat-typing')) return; // 이미 있으면 무시

    const el = document.createElement('div');
    el.className = 'chat-msg assistant chat-typing';
    el.innerHTML = '<span class="typing-dots"><span>.</span><span>.</span><span>.</span></span>';
    messagesEl.appendChild(el);
    el.scrollIntoView({ behavior: 'smooth' });
}

function hideTypingIndicator() {
    const el = document.querySelector('.chat-typing');
    if (el) el.remove();
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ═══════════════════════════════════════════════════════════
// SSE — 실시간 업데이트
// ═══════════════════════════════════════════════════════════

function connectSSE() {
    const source = new EventSource('/api/events');

    source.addEventListener('task_updated', () => {
        // detail이 열려있으면 task list 갱신 보류 (닫힐 때 다시 로드됨)
        const hasOpenDetail = document.querySelector('.task-detail-inline');
        if (document.querySelector('.nav-btn.active').dataset.tab === 'tasks' && !hasOpenDetail) loadTasks();
        if (document.querySelector('.nav-btn.active').dataset.tab === 'dashboard') loadDashboard();
    });

    source.addEventListener('project_updated', () => {
        if (document.querySelector('.nav-btn.active').dataset.tab === 'dashboard') loadDashboard();
    });

    source.addEventListener('notification', (e) => {
        // 배지 갱신
        api('/api/notifications?unread_only=true&limit=1').then(resp => {
            const badge = document.getElementById('notification-badge');
            if (resp.unread_count > 0) {
                badge.textContent = resp.unread_count;
                badge.classList.remove('hidden');
            }
        });
        if (document.querySelector('.nav-btn.active').dataset.tab === 'notifications') loadNotifications();

        // Chat 영역에 알림 표시
        if (chatSessionId) {
            try {
                const data = JSON.parse(e.data);
                const notiText = _formatNotificationForChat(data);
                if (notiText) {
                    appendChatMsg('system', notiText);
                }
            } catch (err) {
                // 파싱 실패 무시
            }
        }
    });

    source.addEventListener('pr_action_result', (e) => {
        const data = JSON.parse(e.data);
        if (data.success) {
            // 성공 — dashboard/task list 갱신 (카드가 자연스럽게 사라짐)
            loadDashboard();
            loadTasks();
        } else {
            // 실패 — 버튼 복원 + 에러 메시지 표시
            showPrError(data.project, data.task_id, data.error || '알 수 없는 오류');
        }
    });

    // Chat 이벤트
    source.addEventListener('chat_message', (e) => {
        const data = JSON.parse(e.data);
        // 현재 세션의 메시지만 표시
        if (data.session_id !== chatSessionId) return;
        hideTypingIndicator();
        appendChatMsg(data.role, data.content);
    });

    source.addEventListener('chat_typing', (e) => {
        const data = JSON.parse(e.data);
        if (data.session_id !== chatSessionId) return;
        if (data.active) {
            showTypingIndicator();
        } else {
            hideTypingIndicator();
        }
    });

    source.onerror = () => {
        // 자동 재연결 (EventSource 기본 동작)
    };
}

// ═══════════════════════════════════════════════════════════
// 초기 로드
// ═══════════════════════════════════════════════════════════

loadDashboard();
initChatSession();
connectSSE();
