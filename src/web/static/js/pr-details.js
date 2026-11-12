/**
 * Shared "View Details" modal for a single Azure DevOps PR's AI-adoption
 * report (commit-level AI vs manual breakdown + which code areas/files
 * each touched). Used by both the admin Azure DevOps Integration page and
 * the user-facing Git Pull Requests page, so it lives here once instead
 * of being duplicated in each template's inline script.
 */

function ensurePrDetailModal() {
    if (document.getElementById('prDetailOverlay')) return;
    const overlay = document.createElement('div');
    overlay.id = 'prDetailOverlay';
    overlay.className = 'pr-detail-overlay';
    overlay.innerHTML = `
        <div class="pr-detail-panel">
            <div class="pr-detail-header">
                <div>
                    <h3 id="prDetailTitle">Loading…</h3>
                    <div class="text-muted" id="prDetailSubtitle" style="font-size:0.8rem;"></div>
                </div>
                <button class="pr-detail-close" onclick="closePrDetails()" aria-label="Close">&times;</button>
            </div>
            <div class="pr-detail-body" id="prDetailBody"></div>
        </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) closePrDetails();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closePrDetails();
    });
}

function closePrDetails() {
    const overlay = document.getElementById('prDetailOverlay');
    if (overlay) overlay.classList.remove('visible');
}

function escapeHtmlLocal(s) {
    return typeof escapeHtml === 'function' ? escapeHtml(s) : String(s ?? '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function formatHoursLocal(hours) {
    if (hours === null || hours === undefined) return '—';
    if (hours < 1) return `${Math.round(hours * 60)}m`;
    if (hours < 48) return `${hours.toFixed(1)}h`;
    return `${(hours / 24).toFixed(1)}d`;
}

function voteBadgeClass(vote) {
    if (vote >= 10) return 'badge-success';
    if (vote >= 5) return 'badge-success';
    if (vote <= -10) return 'badge-danger';
    if (vote <= -5) return 'badge-warning';
    return 'badge-muted';
}

async function openPrDetails(prRowId) {
    ensurePrDetailModal();
    const overlay = document.getElementById('prDetailOverlay');
    const titleEl = document.getElementById('prDetailTitle');
    const subtitleEl = document.getElementById('prDetailSubtitle');
    const bodyEl = document.getElementById('prDetailBody');
    overlay.classList.add('visible');
    titleEl.textContent = `PR #${prRowId}`;
    subtitleEl.textContent = '';
    bodyEl.innerHTML = '<p class="text-muted">Loading commit + file details…</p>';

    let pr;
    try {
        pr = await fetch(`${ROOT_PATH}/api/integrations/ado/pull-requests/${prRowId}/details`).then(r => r.json());
    } catch (e) {
        bodyEl.innerHTML = `<p class="text-muted">Failed to load PR details: ${escapeHtmlLocal(e)}</p>`;
        return;
    }
    if (pr.error) {
        bodyEl.innerHTML = `<p class="text-muted">${escapeHtmlLocal(pr.error)}</p>`;
        return;
    }

    titleEl.innerHTML = pr.url
        ? `<a href="${pr.url}" target="_blank">#${pr.ado_pr_id}</a> ${escapeHtmlLocal(pr.title || '')}`
        : `#${pr.ado_pr_id} ${escapeHtmlLocal(pr.title || '')}`;
    subtitleEl.textContent = `${pr.ado_project}/${pr.ado_repository} · ${pr.author || 'unknown author'} · ${pr.status}`;

    const pct = pr.ai_commit_pct || 0;
    let html = `
        <div class="stat-row" style="margin-bottom:1rem;">
            <div class="stat-box"><div class="val">${pr.total_commits}</div><div class="lbl">Total Commits</div></div>
            <div class="stat-box"><div class="val">${pr.ai_commits}</div><div class="lbl">AI Commits</div></div>
            <div class="stat-box"><div class="val">${pct}%</div><div class="lbl">AI %</div></div>
            <div class="stat-box"><div class="val">${pr.is_ai_assisted ? '🤖 Yes' : '— No'}</div><div class="lbl">AI-Assisted</div></div>
        </div>
        <div class="pr-ai-bar"><div class="pr-ai-bar-fill" style="width:${pct}%;"></div></div>`;

    if (pr.files_error) {
        html += `<p class="text-muted" style="font-size:0.8rem; margin-top:0.5rem;">⚠ File-level details unavailable: ${escapeHtmlLocal(pr.files_error)}. AI/commit stats above are still accurate.</p>`;
    }

    // People: author, co-authors, reviewers/approvals, who completed it.
    html += '<h4 style="margin-top:1.5rem;">People</h4>';
    html += '<div class="pr-detail-grid">';
    html += `<div><span class="text-muted">Author</span><div>${escapeHtmlLocal(pr.author || '—')}</div></div>`;
    html += `<div><span class="text-muted">Co-author(s)</span><div>${(pr.co_authors && pr.co_authors.length) ? pr.co_authors.map(escapeHtmlLocal).join(', ') : '—'}</div></div>`;
    html += `<div><span class="text-muted">Completed/Closed by</span><div>${escapeHtmlLocal(pr.closed_by || '—')}</div></div>`;
    html += '</div>';

    if (pr.reviewers && pr.reviewers.length) {
        html += '<table style="width:100%; margin-top:0.75rem;"><thead><tr><th>Reviewer</th><th>Vote</th></tr></thead><tbody>';
        pr.reviewers.forEach(r => {
            html += `<tr><td>${escapeHtmlLocal(r.display_name || '')}${r.is_required ? ' <span class="text-muted" style="font-size:0.75rem;">(required)</span>' : ''}</td>
                <td><span class="badge ${voteBadgeClass(r.vote)}">${escapeHtmlLocal(r.vote_label)}</span></td></tr>`;
        });
        html += '</tbody></table>';
    } else {
        html += '<p class="text-muted" style="font-size:0.85rem;">No reviewers recorded for this PR.</p>';
    }

    // Timeline: created -> approved -> completed, with durations.
    html += '<h4 style="margin-top:1.5rem;">Timeline</h4>';
    html += `<div class="stat-row">
            <div class="stat-box"><div class="val" style="font-size:1rem;">${pr.created_date ? formatLocalDateTime(pr.created_date) : '—'}</div><div class="lbl">Created</div></div>
            <div class="stat-box"><div class="val">${formatHoursLocal(pr.time_to_approve_hours)}</div><div class="lbl">Time to Approve</div></div>
            <div class="stat-box"><div class="val">${formatHoursLocal(pr.time_to_complete_hours)}</div><div class="lbl">Time to Complete</div></div>
            <div class="stat-box"><div class="val" style="font-size:1rem;">${pr.closed_date ? formatLocalDateTime(pr.closed_date) : '—'}</div><div class="lbl">Closed</div></div>
        </div>`;
    html += '<p class="text-muted" style="font-size:0.8rem;">Time to approve is best-effort: Azure DevOps does not officially document a stable schema for vote-timestamp events, so this is derived from the PR\'s comment thread history and may be unavailable for some PRs.</p>';
    if (pr.threads_error) {
        html += `<p class="text-muted" style="font-size:0.8rem;">⚠ Feedback/approval timing unavailable: ${escapeHtmlLocal(pr.threads_error)}</p>`;
    }

    // Feedback: real (non-system) review comments.
    html += '<h4 style="margin-top:1.5rem;">PR Feedback</h4>';
    if (pr.feedback && pr.feedback.length) {
        html += '<div class="pr-feedback-list">';
        pr.feedback.forEach(c => {
            html += `<div class="pr-feedback-item${c.is_deleted ? ' pr-feedback-deleted' : ''}">
                <div class="pr-feedback-meta"><strong>${escapeHtmlLocal(c.author || 'Unknown')}</strong> <span class="text-muted">${c.published_date ? formatLocalDateTime(c.published_date) : ''}</span></div>
                <div class="pr-feedback-content">${escapeHtmlLocal(c.content || '')}</div>
            </div>`;
        });
        html += '</div>';
    } else {
        html += '<p class="text-muted" style="font-size:0.85rem;">No review comments on this PR.</p>';
    }

    html += '<h4 style="margin-top:1.5rem;">Code areas touched</h4>';
    if (pr.code_areas && pr.code_areas.length) {
        html += '<p class="text-muted" style="font-size:0.8rem;">Which folders were changed by AI-assisted commits vs manual commits.</p>';
        pr.code_areas.forEach(area => {
            const chips = [
                ...area.ai_only_files.map(f => `<span class="pr-file-chip pr-file-chip-ai" title="${escapeHtmlLocal(f)}">🤖 ${escapeHtmlLocal(f.split('/').pop())}</span>`),
                ...area.both_files.map(f => `<span class="pr-file-chip pr-file-chip-both" title="${escapeHtmlLocal(f)}">◐ ${escapeHtmlLocal(f.split('/').pop())}</span>`),
                ...area.manual_only_files.map(f => `<span class="pr-file-chip pr-file-chip-manual" title="${escapeHtmlLocal(f)}">${escapeHtmlLocal(f.split('/').pop())}</span>`),
            ].join('');
            html += `<div class="pr-code-area-row">
                <code>${escapeHtmlLocal(area.area)}</code>
                <span class="text-muted">${area.total_files} file${area.total_files === 1 ? '' : 's'}</span>
                <div>${chips}</div>
            </div>`;
        });
    } else {
        html += '<p class="text-muted">No file-level data available for this PR yet.</p>';
    }

    html += '<h4 style="margin-top:1.5rem;">Commits</h4>';
    html += '<table style="width:100%;"><thead><tr><th>SHA</th><th>Author</th><th>Date</th><th>Message</th><th>Files</th><th>Flag</th></tr></thead><tbody>';
    (pr.commits || []).forEach(c => {
        const badge = c.is_ai
            ? `<span class="badge badge-success">🤖 AI</span>`
            : `<span class="text-muted" style="font-size:0.8rem;">— Manual</span>`;
        html += `<tr>
            <td><code class="small-code">${escapeHtmlLocal((c.commit_sha || '').slice(0, 8))}</code></td>
            <td>${escapeHtmlLocal(c.author_email || '')}</td>
            <td>${c.commit_date ? new Date(c.commit_date).toLocaleDateString() : ''}</td>
            <td>${escapeHtmlLocal((c.comment || '').split('\n')[0]).slice(0, 80)}</td>
            <td>${(c.files || []).length}</td>
            <td>${badge}</td>
        </tr>`;
    });
    html += '</tbody></table>';

    bodyEl.innerHTML = html;
}

