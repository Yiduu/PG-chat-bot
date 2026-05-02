## ✅ Detailed Fix for Feed, Edit Profile, and Settings

Here are the **exact code corrections** for the three issues.

---

### 1. **Fix Feed – SQL Error in `mini_app_get_posts`**

The error `column r.post_id does not exist` indicates that your deployed version has a wrong subquery. Replace the entire `mini_app_get_posts` function with this corrected version:

```python
@flask_app.route('/api/mini-app/get-posts', methods=['GET'])
def mini_app_get_posts():
    """API endpoint for getting posts from mini app - With Pagination and Unread Counts"""
    try:
        user_id = request.args.get('user_id')
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 10))
        offset = (page - 1) * per_page
        
        # Get approved posts
        posts = db_fetch_all('''
            SELECT 
                p.post_id,
                p.content,
                p.timestamp,
                p.comment_count,
                p.media_type,
                u.user_id as author_id,
                u.sex as author_sex,
                u.avatar_emoji as author_avatar,
                u.anonymous_name as author_name,
                STRING_AGG(DISTINCT pc.category_code, ',') as categories,
                COALESCE((
                    SELECT COUNT(*) 
                    FROM comments c2 
                    WHERE c2.post_id = p.post_id 
                    AND c2.timestamp > COALESCE((
                        SELECT last_viewed FROM post_views pv 
                        WHERE pv.user_id = %s AND pv.post_id = p.post_id
                    ), '1970-01-01')
                ), 0) as unread_comments
            FROM posts p
            JOIN users u ON p.author_id = u.user_id
            LEFT JOIN post_categories pc ON p.post_id = pc.post_id
            WHERE p.approved = TRUE
            GROUP BY p.post_id, u.user_id, u.sex, u.avatar_emoji, u.anonymous_name
            ORDER BY p.timestamp DESC
            LIMIT %s OFFSET %s
        ''', (user_id, per_page, offset))
        
        # If no posts, return empty list
        if posts is None:
            posts = []
        
        formatted_posts = []
        for post in posts:
            if isinstance(post['timestamp'], str):
                post_time = datetime.strptime(post['timestamp'], '%Y-%m-%d %H:%M:%S')
            else:
                post_time = post['timestamp']
            
            now = datetime.now()
            time_diff = now - post_time
            
            if time_diff.days > 0:
                time_ago = f"{time_diff.days}d ago"
            elif time_diff.seconds > 3600:
                time_ago = f"{time_diff.seconds // 3600}h ago"
            elif time_diff.seconds > 60:
                time_ago = f"{time_diff.seconds // 60}m ago"
            else:
                time_ago = "Just now"
            
            content_preview = post['content']
            if len(content_preview) > 300:
                content_preview = content_preview[:297] + '...'
            
            rating = calculate_user_rating(post['author_id'])
            aura_sticker = format_aura(rating)
            
            category_list = post['categories'].split(',') if post['categories'] else ['Other']
            
            formatted_posts.append({
                'id': post['post_id'],
                'content': content_preview,
                'full_content': post['content'],
                'categories': category_list,
                'time_ago': time_ago,
                'comments': post['comment_count'] or 0,
                'unread_comments': post['unread_comments'],
                'author': {
                    'name': 'Anonymous',
                    'sex': post['author_sex'] or '👤',
                    'avatar': post['author_avatar'] or "",
                    'aura': aura_sticker,
                    'is_me': str(post['author_id']) == str(user_id)
                },
                'has_media': post['media_type'] != 'text'
            })

        total_posts = db_fetch_one("SELECT COUNT(*) as count FROM posts WHERE approved = TRUE")
        
        return jsonify({
            'success': True,
            'data': formatted_posts,
            'page': page,
            'total_posts': total_posts['count'] if total_posts else 0,
            'has_more': len(posts) == per_page,
            'next_page': page + 1 if len(posts) == per_page else None
        })
        
    except Exception as e:
        logger.error(f"Error in mini-app get posts: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
```

---

### 2. **Fix Edit Profile – Handle Empty Avatar Correctly**

Update your `mini_app_update_profile` function:

```python
@flask_app.route('/api/mini-app/profile/<user_id>', methods=['PUT'])
def mini_app_update_profile(user_id):
    """API endpoint for updating user profile"""
    try:
        data = request.get_json()
        name = data.get('name', '').strip()
        bio = data.get('bio', '').strip()
        avatar = data.get('avatar', '').strip()
        
        if not name:
            return jsonify({'success': False, 'error': 'Name is required'}), 400
        
        # Convert empty string to None (NULL in database)
        avatar = None if avatar == '' else avatar
            
        db_execute(
            "UPDATE users SET anonymous_name = %s, bio = %s, avatar_emoji = %s WHERE user_id = %s",
            (name, bio, avatar, user_id)
        )
        
        # Clear caches
        calculate_user_rating.cache_clear()
        format_aura.cache_clear()
        
        return jsonify({'success': True, 'message': 'Profile updated successfully'})
    except Exception as e:
        logger.error(f"Profile update error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
```

---

### 3. **Fix Settings Endpoints**

Ensure your settings endpoints are correct:

```python
@flask_app.route('/api/mini-app/settings/<user_id>', methods=['GET'])
def mini_app_get_settings(user_id):
    """API endpoint for fetching user settings"""
    try:
        user = db_fetch_one("SELECT notifications_enabled, privacy_public FROM users WHERE user_id = %s", (user_id,))
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404
            
        return jsonify({
            'success': True,
            'data': {
                'notifications': user['notifications_enabled'],
                'privacy_public': user['privacy_public']
            }
        })
    except Exception as e:
        logger.error(f"Error fetching settings: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@flask_app.route('/api/mini-app/settings/<user_id>', methods=['POST'])
def mini_app_update_settings(user_id):
    """API endpoint for updating user settings"""
    try:
        data = request.get_json()
        notifications = data.get('notifications')
        privacy_public = data.get('privacy_public')
        
        updates = []
        params = []
        
        if notifications is not None:
            updates.append("notifications_enabled = %s")
            params.append(notifications)
            
        if privacy_public is not None:
            updates.append("privacy_public = %s")
            params.append(privacy_public)
            
        if not updates:
            return jsonify({'success': False, 'error': 'No settings to update'}), 400
            
        params.append(user_id)
        db_execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id = %s", tuple(params))
        
        return jsonify({'success': True, 'message': 'Settings updated'})
    except Exception as e:
        logger.error(f"Error updating settings: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
```

---

### 4. **Frontend JavaScript Fixes (if needed)**

Check that `loadSettings()` and `saveSettings()` are correctly implemented. In your `mini_app_page()` script, ensure:

```javascript
async function loadSettings() {
  try {
    const data = await apiFetch(`/api/mini-app/settings/${state.userId}`);
    if (data.success && data.data) {
      document.getElementById('set-notifications').checked = data.data.notifications;
      document.getElementById('set-privacy').checked = data.data.privacy_public;
    }
  } catch(e) { console.error('Settings load error:', e); }
}

async function saveSettings() {
  const btn = document.getElementById('saveSettingsBtn');
  if (!btn) return;
  btn.disabled = true;
  try {
    await apiFetch(`/api/mini-app/settings/${state.userId}`, {
      method: 'POST',
      body: JSON.stringify({
        notifications: document.getElementById('set-notifications').checked,
        privacy_public: document.getElementById('set-privacy').checked
      })
    });
    toast('Settings saved');
  } catch(e) { toast(e.message); }
  finally { btn.disabled = false; }
}
```

Also ensure `saveProfile()` sends `avatar` as empty string when removed:

```javascript
async function saveProfile() {
  const name = document.getElementById('edit-name').value.trim();
  const bio = document.getElementById('edit-bio').value.trim();
  if(!name) return toast('Name required');
  const btn = document.getElementById('saveProfileBtn');
  btn.disabled = true;
  try {
    await apiFetch(`/api/mini-app/profile/${state.userId}`, {
      method: 'PUT',
      body: JSON.stringify({ name, bio, avatar: state.selectedEmoji || '' })
    });
    toast('Profile updated');
    switchPage('profile');
    await loadProfile();
  } catch(e) { toast(e.message); }
  finally { btn.disabled = false; }
}
```

---

After applying these changes, restart your Flask server. The feed should load, edit profile should work, and settings should toggle correctly.
