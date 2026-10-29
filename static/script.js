'use strict';

let lastAnalysis = null;

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

function markdownToHtml(value) {
    const escaped = escapeHtml(value);
    return escaped
        .replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/\*(.+?)\*/g, '<em>$1</em>')
        .replace(/\n/g, '<br>');
}

function renderSafeHtml(value) {
    return markdownToHtml(value);
}

function renderRefactorDiff(original, refactored) {
    const originalText = String(original ?? '');
    const refactoredText = String(refactored ?? '');
    if (!refactoredText) return '<div class="diff-empty">No refactored code available.</div>';
    if (originalText === refactoredText) {
        return '<div class="diff-identical">Refactored code is identical to the original.</div>' +
            '<pre class="code-output">' + escapeHtml(originalText) + '</pre>';
    }
    return '<div class="diff-container">' +
        '<div class="diff-panel original"><h4 class="diff-header">Original</h4>' +
        '<pre class="code-output">' + escapeHtml(originalText) + '</pre></div>' +
        '<div class="diff-panel refactored"><h4 class="diff-header">Refactored</h4>' +
        '<pre class="code-output">' + escapeHtml(refactoredText) + '</pre></div></div>';
}

function switchTab(tabName) {
    ['review', 'bugs', 'refactor', 'docs'].forEach(name => {
        const btn = document.getElementById('tab-btn-' + name);
        const pane = document.getElementById('tab-' + name);
        if (btn) btn.classList.toggle('active', name === tabName);
        if (pane) pane.classList.toggle('active', name === tabName);
    });
}

function showLoader(tabName, show) {
    const loader = document.getElementById('loader-' + tabName);
    if (!loader) return;
    loader.classList.toggle('hidden', !show);
    loader.classList.toggle('visible', show);
}

function hideAllLoaders() {
    ['review', 'bugs', 'refactor', 'docs'].forEach(name => showLoader(name, false));
}

function setText(id, value) {
    const element = document.getElementById(id);
    if (element) element.textContent = String(value ?? '');
}

function setSafeMarkdown(id, value) {
    const element = document.getElementById(id);
    if (element) element.innerHTML = renderSafeHtml(value);
}

function filenameForLanguage(language) {
    const extensions = {
        python: 'py', java: 'java', cpp: 'cpp', 'c++': 'cpp',
        javascript: 'js', js: 'js', typescript: 'ts', ts: 'ts'
    };
    return 'main.' + (extensions[language] || 'txt');
}

async function analyzeCode() {
    const button = document.getElementById('analyze-btn');
    const content = document.getElementById('content')?.value ?? '';
    const language = document.getElementById('language')?.value ?? 'python';

    if (!content.trim()) {
        setText('review-output', 'Please paste some source code before starting analysis.');
        switchTab('review');
        return;
    }

    if (button) button.disabled = true;
    hideAllLoaders();
    ['review', 'bugs', 'refactor', 'docs'].forEach(name => showLoader(name, true));
    setText('review-output', 'Analysis in progress…');
    setText('bugs-output', 'Checking for bugs and vulnerabilities…');
    setText('refactor-output', 'Preparing refactoring results…');
    setText('docs-output', 'Preparing documentation…');
    switchTab('review');

    try {
        const response = await fetch('/analyze', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            body: JSON.stringify({ filename: filenameForLanguage(language), language, content })
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) {
            const detail = result.detail;
            throw new Error(typeof detail === 'string' ? detail :
                (detail?.message || result.message || `Analysis failed (HTTP ${response.status}).`));
        }

        lastAnalysis = { ...result, filename: filenameForLanguage(language), language, content };
        setSafeMarkdown('review-output', result.review || 'No review was returned.');
        setSafeMarkdown('bugs-output', result.bugs || 'No bug analysis was returned.');
        const refactorElement = document.getElementById('refactor-output');
        if (refactorElement) {
            refactorElement.innerHTML = renderRefactorDiff(content, result.refactored || content);
        }
        setText('docs-output', result.docs || 'No documentation was returned.');
        setText('health-score-pill', `Score: ${result.health_score ?? result.metrics?.health_score ?? '--'}`);
        setText('risk-badge', `Risk: ${result.risk_level ?? result.metrics?.risk_level ?? '--'}`);
    } catch (error) {
        const message = error instanceof Error ? error.message : 'An unexpected error occurred.';
        setText('review-output', `Analysis failed: ${message}`);
        setText('bugs-output', 'Bug analysis unavailable because the request failed.');
        setText('refactor-output', 'Refactoring unavailable because the request failed.');
        setText('docs-output', 'Documentation unavailable because the request failed.');
        lastAnalysis = null;
        switchTab('review');
    } finally {
        hideAllLoaders();
        if (button) button.disabled = false;
    }
}

async function downloadReport() {
    if (!lastAnalysis) {
        setText('review-output', 'Run an analysis successfully before exporting a report.');
        switchTab('review');
        return;
    }

    const button = document.getElementById('export-btn');
    if (button) button.disabled = true;
    try {
        const response = await fetch('/export/markdown', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            body: JSON.stringify(lastAnalysis)
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) {
            const detail = result.detail;
            throw new Error(typeof detail === 'string' ? detail :
                (detail?.message || `Export failed (HTTP ${response.status}).`));
        }
        const blob = new Blob([String(result.content ?? '')], { type: 'text/markdown;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = result.filename || 'CodeNexus_Audit_Report.md';
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        URL.revokeObjectURL(url);
    } catch (error) {
        setText('review-output', `Export failed: ${error instanceof Error ? error.message : 'Unexpected error.'}`);
        switchTab('review');
    } finally {
        if (button) button.disabled = false;
    }
}

window.analyzeCode = analyzeCode;
window.downloadReport = downloadReport;
window.switchTab = switchTab;
