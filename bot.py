  };
  box.innerHTML = roots.map(c => renderC(c, 0)).join('');
}

function toggleThread(id) {
  if(state.collapsedThreads.has(id)) state.collapsedThreads.delete(id);
  else state.collapsedThreads.add(id);
  renderComments();
}

async function submitReply(parentId) {
  const text = document.getElementById('reply-text-'+parentId).value.trim();
  if(!text) return toast('Write something');
  try {
    await apiFetch(`/api/mini-app/post/${state.currentPostId}/comment`, {
      method: 'POST', body: JSON.stringify({ user_id: state.userId, content: text, parent_comment_id: parentId })
    });
    toast('Reply posted');
    await loadComments(state.currentPostId);
  } catch(e) { toast(e.message); }
}

// REST OF COMPONENTS (Leaderboard, Categories, Profile, etc.)
function renderCategories() {
  const container = document.getElementById('categoriesContainer');
  container.innerHTML = CONFIG.categoryGroups.map(g => `
    <div class="card" style="margin-bottom:8px; padding:10px;">
      <div style="font-size:0.85rem; font-weight:700; margin-bottom:8px;">${g.name}</div>
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px;">
        ${CONFIG.categories.filter(([code]) => g.cats.includes(code)).map(([code, label]) => `
          <div class="cat-btn ${state.selectedCategories.has(code)?'selected':''}" data-code="${code}" style="padding:6px; font-size:0.75rem; border:1px solid var(--border); border-radius:6px; cursor:pointer;" onclick="toggleCat(this, '${code}')">
            ${esc(label)}
          </div>
        `).join('')}
