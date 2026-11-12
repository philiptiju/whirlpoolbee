# Testing Guide: New Features

This guide covers testing for three newly implemented feature sets:
- **Feature #16**: Learning & Development
- **Feature #15**: AI Usage Optimization
- **Feature #6**: Code Analytics

## 1. Server Startup

Start the FastAPI server:

```bash
cd /path/to/claude-local-dashboard
python -m uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000
```

Expected output:
```
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
INFO:     Started reloader process
INFO:     Started server process
INFO:     Waiting for application startup.
INFO:     Application startup complete.
```

Access the dashboard at: `http://localhost:8000`

## 2. UI Page Testing

### 2.1 Learning & Development Page (`/learning`)

**Navigate to:** `http://localhost:8000/learning`

**Expected Elements:**
- Page header: "Learning & Development"
- 4 summary stat cards (Best Practices, Active Paths, Skills Tracked, Achievements)
- 5 tabs: Best Practices, Skills Tracker, Learning Paths, Community Patterns, Achievements
- Filter dropdowns for category and difficulty
- Machine selector sidebar

**Tab Testing:**

1. **Best Practices Tab** (active by default)
   - Should display grid of practice cards
   - Each card shows: category badge, title, description, example prompt/code
   - Difficulty level indicator
   - Filter by category (prompting, debugging, refactoring, optimization, testing)
   - Filter by difficulty (beginner, intermediate, advanced)

2. **Skills Tracker Tab**
   - Progress bars for each skill
   - Current level and XP display
   - Practice count and success rate
   - Last practiced timestamp
   - Visual progress indicators

3. **Learning Paths Tab**
   - List of available learning paths
   - Difficulty badges
   - Step count for each path
   - Description and objectives
   - Completion status if user has started

4. **Community Patterns Tab**
   - Shared code patterns from community
   - Language badges
   - Upvote counts
   - Code examples with syntax highlighting
   - Filter by pattern type and language

5. **Achievements Tab**
   - Achievement cards with unlock status
   - Unlock date/time
   - Achievement descriptions
   - Visual indicators for unlocked vs locked

### 2.2 AI Usage Optimization Page (`/optimization`)

**Navigate to:** `http://localhost:8000/optimization`

**Expected Elements:**
- Page header: "AI Usage Optimization"
- 4 summary stat cards (Avg Prompt Quality, Cache Savings, Cache Hit Rate, Token Waste)
- 5 tabs: Prompt Analysis, Context Optimization, Cache Analytics, Model Performance, Token Waste
- Various filter dropdowns

**Tab Testing:**

1. **Prompt Analysis Tab** (active by default)
   - Prompt quality cards with scores
   - Quality breakdown: clarity, specificity, context efficiency, token efficiency
   - Suggestions for improvement
   - Improved prompt examples
   - Filter by quality level (high 80+, medium 60-80, low <60)

2. **Context Optimization Tab**
   - Context waste chart (bar chart)
   - Insight cards showing token distribution
   - Waste percentage indicators
   - Cost savings estimates
   - Recommendations for optimization

3. **Cache Analytics Tab**
   - Cache efficiency chart (line chart)
   - Cache performance cards
   - Hit rate percentages
   - Cost saved calculations
   - Efficiency progress bars

4. **Model Performance Tab**
   - Model comparison chart (bar chart)
   - Performance cards per model
   - Quality scores, latency, success rates
   - Cost per request metrics
   - Filter by task type (code_generation, debugging, refactoring, documentation)

5. **Token Waste Tab**
   - Waste patterns pie chart
   - Pattern cards with occurrence counts
   - Cost impact estimates
   - Recommendations for reducing waste

### 2.3 Code Analytics Page (`/analytics`)

**Navigate to:** `http://localhost:8000/analytics`

**Expected Elements:**
- Page header: "Code Analytics"
- 4 summary stat cards (Code Modifications, Avg Code Quality, AI Acceptance Rate, Top Language)
- 5 tabs: File Heatmap, Code Quality, Language Breakdown, AI Suggestions, Refactoring Impact
- Time range filters (7/30/90 days)

**Tab Testing:**

1. **File Heatmap Tab** (active by default)
   - Table with file paths, languages, modification counts
   - Visual heat bars showing activity
   - Lines added/deleted with color coding
   - Average quality scores
   - Sort by modification frequency

2. **Code Quality Tab**
   - Quality trend chart (line chart)
   - Quality metric cards per session
   - Test coverage percentages
   - Cyclomatic complexity scores
   - Maintainability index
   - Code duplication percentages

3. **Language Breakdown Tab**
   - Language distribution pie chart
   - Language stat cards
   - Session counts per language
   - Lines added/deleted totals
   - Average quality by language

4. **AI Suggestions Tab**
   - Acceptance rate chart (bar chart)
   - Suggestion cards with status badges (Accepted/Rejected/Modified)
   - Before/after code snippets
   - Acceptance time metrics
   - Filter by suggestion type (code_completion, refactoring, bug_fix, optimization)

5. **Refactoring Impact Tab**
   - Before/after quality comparisons
   - Improvement indicators with percentage changes
   - Quality score progressions
   - Test coverage improvements
   - Complexity reduction metrics

## 3. API Endpoint Testing

### 3.1 Learning & Development APIs

#### Get Best Practices
```bash
curl "http://localhost:8000/api/learning/best-practices?category=prompting&limit=10"
```

Expected response: JSON array of best practice objects

#### Create Best Practice
```bash
curl -X POST "http://localhost:8000/api/learning/best-practices" \
  -H "Content-Type: application/json" \
  -d '{
    "category": "prompting",
    "title": "Be Specific with Context",
    "description": "Provide clear, specific context in your prompts",
    "example_prompt": "Instead of '\''fix bug'\'', use '\''fix the authentication timeout bug in login.py'\''",
    "difficulty_level": "beginner",
    "tags": "specificity,clarity,context"
  }'
```

Expected response: `{"status": "created", "id": <new_id>}`

#### Get User Skills
```bash
curl "http://localhost:8000/api/learning/skills/user123"
```

Expected response: JSON array of skill objects with levels and XP

#### Get Learning Paths
```bash
curl "http://localhost:8000/api/learning/paths?difficulty=intermediate"
```

Expected response: JSON array of learning path objects

#### Get Learning Progress
```bash
curl "http://localhost:8000/api/learning/progress/user123"
```

Expected response: JSON array of progress objects

#### Get Community Patterns
```bash
curl "http://localhost:8000/api/learning/community-patterns?language=python&limit=20"
```

Expected response: JSON array of community pattern objects

#### Get User Achievements
```bash
curl "http://localhost:8000/api/learning/achievements/user123"
```

Expected response: JSON array of achievement objects

### 3.2 AI Usage Optimization APIs

#### Get Prompt Analysis
```bash
curl "http://localhost:8000/api/optimization/prompt-analysis?min_quality=70&limit=20"
```

Expected response: JSON array of prompt analysis objects

#### Create Prompt Analysis
```bash
curl -X POST "http://localhost:8000/api/optimization/prompt-analysis" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "session123",
    "prompt_text": "Write a function to sort an array",
    "quality_score": 65,
    "clarity_score": 70,
    "specificity_score": 60,
    "context_efficiency_score": 65,
    "token_efficiency": 75.5,
    "suggestions": "Add details about sort algorithm preference and input constraints"
  }'
```

Expected response: `{"status": "created", "id": <new_id>}`

#### Get Context Insights
```bash
curl "http://localhost:8000/api/optimization/context-insights?days=30"
```

Expected response: JSON array of context insight objects

#### Create Context Insight
```bash
curl -X POST "http://localhost:8000/api/optimization/context-insights" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "session123",
    "total_context_tokens": 5000,
    "unique_context_tokens": 3500,
    "redundant_tokens": 1500,
    "optimal_tokens": 3200,
    "waste_percentage": 30.0,
    "estimated_cost_savings": 0.0025,
    "recommendations": "Remove duplicate file includes and consolidate context"
  }'
```

Expected response: `{"status": "created", "id": <new_id>}`

#### Get Cache Analytics
```bash
curl "http://localhost:8000/api/optimization/cache-analytics?days=30"
```

Expected response: JSON array of cache analytics objects

#### Create Cache Analytics
```bash
curl -X POST "http://localhost:8000/api/optimization/cache-analytics" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "session123",
    "cache_read_tokens": 2000,
    "cache_write_tokens": 500,
    "cache_hit_rate": 75.5,
    "cache_efficiency_score": 82,
    "cost_saved_usd": 0.0035
  }'
```

Expected response: `{"status": "created", "id": <new_id>}`

#### Get Model Performance
```bash
curl "http://localhost:8000/api/optimization/model-performance?model_name=claude-sonnet-4-5&task_type=code_generation&days=30"
```

Expected response: JSON array of model performance metric objects

#### Get Token Waste Patterns
```bash
curl "http://localhost:8000/api/optimization/token-waste?days=30"
```

Expected response: JSON array of token waste pattern objects

#### Get Optimization Summary
```bash
curl "http://localhost:8000/api/optimization/summary?days=30"
```

Expected response: JSON object with aggregated optimization metrics

### 3.3 Code Analytics APIs

#### Get Code Modifications
```bash
curl "http://localhost:8000/api/analytics/code-modifications?language=python&days=30"
```

Expected response: JSON array of code modification objects

#### Create Code Modification
```bash
curl -X POST "http://localhost:8000/api/analytics/code-modifications" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "session123",
    "file_path": "src/utils/helpers.py",
    "language": "python",
    "modification_type": "refactoring",
    "lines_added": 25,
    "lines_deleted": 18,
    "modification_quality_score": 85
  }'
```

Expected response: `{"status": "created", "id": <new_id>}`

#### Get Language Stats
```bash
curl "http://localhost:8000/api/analytics/language-stats/user123"
```

Expected response: JSON array of language usage statistics

#### Get Code Quality Metrics
```bash
curl "http://localhost:8000/api/analytics/code-quality?days=30"
```

Expected response: JSON array of code quality metric objects

#### Create Code Quality Metrics
```bash
curl -X POST "http://localhost:8000/api/analytics/code-quality" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "session123",
    "overall_quality_score": 82,
    "test_coverage_percentage": 75.5,
    "cyclomatic_complexity": 12,
    "code_duplication_percentage": 5.2,
    "maintainability_index": 78,
    "quality_improvement_percentage": 15.5
  }'
```

Expected response: `{"status": "created", "id": <new_id>}`

#### Get AI Suggestions
```bash
curl "http://localhost:8000/api/analytics/ai-suggestions?suggestion_type=refactoring&days=30"
```

Expected response: JSON array of AI suggestion objects

#### Create AI Suggestion
```bash
curl -X POST "http://localhost:8000/api/analytics/ai-suggestions" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "session123",
    "suggestion_type": "refactoring",
    "suggestion_text": "Extract repeated validation logic into a separate function",
    "code_before": "if x > 0 and x < 100: ...",
    "code_after": "if is_valid_range(x, 0, 100): ...",
    "was_accepted": true,
    "was_modified": false,
    "acceptance_time_seconds": 45
  }'
```

Expected response: `{"status": "created", "id": <new_id>}`

#### Get AI Acceptance Rate
```bash
curl "http://localhost:8000/api/analytics/ai-acceptance-rate?days=30&suggestion_type=refactoring"
```

Expected response: JSON object with acceptance rate statistics

#### Get Project Health
```bash
curl "http://localhost:8000/api/analytics/project-health?working_directory=/path/to/project"
```

Expected response: JSON object with project health metrics

#### Get File Heatmap
```bash
curl "http://localhost:8000/api/analytics/file-heatmap?days=30"
```

Expected response: JSON array of file modification heatmap data

#### Get Language Breakdown
```bash
curl "http://localhost:8000/api/analytics/language-breakdown?days=30"
```

Expected response: JSON array of language breakdown statistics

#### Get Analytics Summary
```bash
curl "http://localhost:8000/api/analytics/summary?days=30"
```

Expected response: JSON object with aggregated analytics metrics

## 4. Integration Testing

### Navigation Testing
1. Click "Learning" link in navbar → Should navigate to `/learning`
2. Click "AI Optimization" link in navbar → Should navigate to `/optimization`
3. Click "Code Analytics" link in navbar → Should navigate to `/analytics`
4. Verify active state highlighting on current page
5. Verify navigation persists selected machine ID in URL

### Machine ID Filtering
1. Select a machine from sidebar
2. Verify `?machine_id=<id>` appears in URL
3. Navigate to different pages
4. Verify machine ID parameter persists across pages
5. Verify data filters correctly for selected machine
6. Select "All Machines" → Verify parameter is removed

### Tab Switching
1. On each page, click through all 5 tabs
2. Verify tab content switches correctly
3. Verify only one tab is active at a time
4. Verify tab state doesn't persist across page navigations
5. Verify tab data loads when switching tabs

### Chart Rendering
1. Verify charts load on appropriate tabs
2. Check browser console for Chart.js errors
3. Verify chart data updates when filters change
4. Verify charts are responsive to window resize
5. Verify chart legends and tooltips work

### Filter Functionality
1. Test category/difficulty filters on Learning page
2. Test quality filter on Optimization page
3. Test task type filter on Optimization page
4. Test suggestion type filter on Analytics page
5. Test time range filters (7/30/90 days)
6. Verify filter changes update displayed data

## 5. Database Verification

Connect to SQLite database:
```bash
sqlite3 ~/.claude-dashboard/usage.db
```

Check that all new tables exist:
```sql
.tables
```

Expected tables (should include):
- best_practices
- user_skills
- learning_paths
- learning_path_steps
- user_learning_progress
- community_patterns
- user_achievements
- prompt_analyses
- context_insights
- cache_analytics
- model_performance_metrics
- token_waste_patterns
- code_modifications
- user_language_stats
- code_quality_metrics
- ai_suggestions

Verify table schemas:
```sql
.schema best_practices
.schema prompt_analyses
.schema code_modifications
```

## 6. Common Issues and Solutions

### Issue: "No data available" messages on pages

**Cause:** Fresh installation with no data in new tables

**Solution:** Use POST endpoints to create sample data:
```bash
# Create a best practice
curl -X POST "http://localhost:8000/api/learning/best-practices" \
  -H "Content-Type: application/json" \
  -d '{"category":"prompting","title":"Test Practice","description":"Test","difficulty_level":"beginner"}'

# Create a prompt analysis
curl -X POST "http://localhost:8000/api/optimization/prompt-analysis" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"test123","prompt_text":"Test prompt","quality_score":75,"clarity_score":80,"specificity_score":70,"context_efficiency_score":75,"token_efficiency":78.5}'

# Create a code modification
curl -X POST "http://localhost:8000/api/analytics/code-modifications" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"test123","file_path":"test.py","language":"python","modification_type":"feature","lines_added":10,"lines_deleted":5,"modification_quality_score":82}'
```

### Issue: Charts not rendering

**Cause:** Chart.js not loading or JavaScript errors

**Solution:**
1. Check browser console for errors
2. Verify Chart.js CDN is accessible
3. Clear browser cache and reload
4. Verify tab has data before rendering chart

### Issue: Machine ID filter not working

**Cause:** localStorage or URL parameter issues

**Solution:**
1. Clear localStorage: `localStorage.clear()` in browser console
2. Manually navigate with `?machine_id=<id>` in URL
3. Check browser console for JavaScript errors
4. Verify machine exists in database

## 7. Success Criteria

All features are working correctly if:

- ✅ All 3 new pages load without errors
- ✅ All 15 tabs (5 per page) display correctly
- ✅ All 27 API endpoints return valid JSON responses
- ✅ Charts render correctly on all tabs that have them
- ✅ Filters update displayed data correctly
- ✅ Machine ID filtering persists across pages
- ✅ POST endpoints successfully create new records
- ✅ Database tables exist with correct schemas
- ✅ Navigation links work and show active states
- ✅ No JavaScript errors in browser console

---

**Testing Complete!** All three feature sets (#16, #15, #6) are now fully integrated into the Claude Local Dashboard.
