        // --- UI 逻辑 ---
        function switchTab(tabName) {
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            const activeBtn = document.querySelector(`.tab-btn[onclick*="'${tabName}'"]`);
            if (activeBtn) activeBtn.classList.add('active');
            document.querySelectorAll('.tab-panel').forEach(panel => panel.classList.remove('active'));
            document.getElementById(tabName + 'Panel').classList.add('active');
            if (tabName === 'crawlers') {
                loadCrawlerConfig();
            } else if (tabName === 'debug') {
                loadDebugCrawlers();
            } else if (tabName === 'console') {
                loadTaskDashboard();
            } else if (tabName === 'tasks') {
                loadTaskCenter();
            }
        }

        function showToast(msg, type = 'success') {
            const toast = document.getElementById('toast');
            toast.textContent = msg;
            toast.className = `toast ${type === 'success' ? 'toast-success' : 'toast-error'} show`;
            setTimeout(() => { toast.classList.remove('show'); }, 3000);
        }

        function setGlobalStatus(label, state) {
            globalStatus.textContent = label;
            globalStatus.className = `status-pill status-${state || 'idle'}`;
        }

        // --- 核心业务逻辑 ---
        const runBtn = document.getElementById('runBtn');
        const statusText = document.getElementById('statusText');
        const globalStatus = document.getElementById('globalStatus');
        const movieListFilter = document.getElementById('movieListFilter');

        let _totalMovies = 0;
        let _runStartedAfter = 0;
        let _movieListScope = 'latest';

        movieListFilter.addEventListener('change', () => {
            _movieListScope = movieListFilter.value || 'latest';
            refreshMovieListFromTasks({scope: _movieListScope});
        });

        runBtn.addEventListener('click', () => {
            clearMovieList();
            _runStartedAfter = Date.now() / 1000 - 2;
            _movieListScope = 'latest';
            movieListFilter.value = 'latest';
            startTaskPolling();
            ensureTaskPolling();
            runBtn.disabled = true;
            runBtn.textContent = '⏳ 程序运行中...';
            statusText.textContent = '任务执行中';
            setGlobalStatus('RUNNING', 'running');
            let isFinished = false;

            const eventSource = new EventSource('/run-app');
            eventSource.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    handleStructuredEvent(data);
                    if (data.type === 'finish') {
                        isFinished = true;
                    }
                } catch (e) {
                    console.error('JSON parse error:', e);
                }
            };
            eventSource.onerror = () => {
                if (!isFinished) {
                    statusText.textContent = '连接中断';
                    finishRun();
                }
                eventSource.close();
            };
        });

        function handleStructuredEvent(data) {
            switch (data.type) {
                case 'start':
                    console.log('JAVSP started with command:', data.command);
                    statusText.textContent = '正在扫描影片...';
                    break;

                case 'scan_complete':
                    _totalMovies = data.total;
                    console.log('scan_complete received:', data.total);
                    const listCountEl = document.getElementById('movieListCount');
                    if (listCountEl) {
                        listCountEl.textContent = `0 / ${data.total}`;
                    }
                    if (data.total === 0) {
                        statusText.textContent = '未找到影片';
                    } else {
                        statusText.textContent = `找到 ${data.total} 部影片，开始刮削...`;
                    }
                    break;

                case 'movie_start':
                    console.log('movie_start received:', data.dvdid, data.num, data.data_src, data.crawlers);
                    addMovieItem(data.dvdid, data.num, data.data_src, data.crawlers);
                    statusText.textContent = `正在刮削: ${data.num}`;
                    break;

                case 'crawler_success':
                    updateMovieCrawlers(data.dvdid, data.crawler, data.fields);
                    break;

                case 'movie_success':
                    console.log('movie_success received:', data);
                    console.log('_movieData before:', JSON.stringify(_movieData));
                    updateMovieStatus(data.dvdid, 'success', data.save_dir);
                    console.log('_movieData after:', JSON.stringify(_movieData));
                    updateMovieCount();
                    const successCount = Object.values(_movieData).filter(m => m.status === 'success').length;
                    statusText.textContent = `刮削成功: ${successCount} / ${_totalMovies}`;
                    break;

                case 'movie_failed':
                    updateMovieStatus(data.dvdid, 'failed');
                    updateMovieCount();
                    const failCount = Object.values(_movieData).filter(m => m.status === 'failed').length;
                    statusText.textContent = `刮削失败: ${failCount} / ${_totalMovies}`;
                    break;

                case 'finish':
                    const summary = data.summary || {};
                    const success = summary.completed || 0;
                    const failed = summary.failed || 0;
                    const total = summary.total || 0;
                    statusText.textContent = `执行完成: ${success} 成功, ${failed} 失败 / 共 ${total} 部`;
                    refreshMovieListFromTasks({scope: 'latest'});
                    finishRun();
                    break;

                case 'error':
                    statusText.textContent = '执行错误: ' + data.message;
                    console.error('JAVSP error:', data.message);
                    finishRun();
                    break;

                case 'log':
                    // 调试用，可以取消注释来查看原始日志
                    // console.log('LOG:', data.content);
                    break;
            }
        }

        function clearMovieList() {
            const container = document.getElementById('movieListContainer');
            container.innerHTML = '<div class="movie-detail-empty">正在扫描影片...</div>';
            document.getElementById('movieListCount').textContent = '0 / 0';
            document.getElementById('movieDetailContent').innerHTML = '<div class="movie-detail-empty">点击左侧列表中的影片查看详情</div>';
            _movieData = {};
            _totalMovies = 0;
        }

        async function clearMovieListAndStatus() {
            clearMovieList();
            try {
                await fetch('/api/clear_scrape_status', {method: 'POST'});
            } catch (e) {
                console.error('Failed to clear scrape status:', e);
            }
        }

        async function restoreScrapeStatus() {
            try {
                const res = await fetch('/api/scrape_status');
                const data = await res.json();
                if (!data.movies || Object.keys(data.movies).length === 0) return;

                _totalMovies = data.total || 0;
                const container = document.getElementById('movieListContainer');
                container.innerHTML = '';

                for (const [dvdid, movie] of Object.entries(data.movies)) {
                    _movieData[dvdid] = {
                        num: movie.num,
                        status: movie.status,
                        crawlers: movie.crawlers || {},
                        save_dir: movie.save_dir,
                        title: movie.title || '',
                        data_src: movie.data_src || 'normal'
                    };

                    const item = document.createElement('div');
                    item.className = `movie-item ${movie.status}`;
                    item.id = `movie-${dvdid}`;
                    item.onclick = () => selectMovie(dvdid);

                    const statusText = movie.status === 'success' ? '成功' : (movie.status === 'failed' ? '失败' : '刮削中');
                    const statusClass = `status-${movie.status}`;

                    item.innerHTML = `
                        <div class="movie-item-header">
                            <span class="movie-item-num">${movie.num}</span>
                            <span class="movie-item-status ${statusClass}">${statusText}</span>
                        </div>
                        <div class="movie-item-title" id="title-${dvdid}">${movie.title || dvdid}</div>
                        <div class="movie-item-crawlers" id="crawlers-${dvdid}"></div>
                    `;
                    container.appendChild(item);

                    // 恢复爬虫标签：影片失败时所有标签显示为失败
                    const crawlersDiv = document.getElementById(`crawlers-${dvdid}`);
                    if (crawlersDiv && movie.crawlers) {
                        for (const [cName, cData] of Object.entries(movie.crawlers)) {
                            const tag = document.createElement('span');
                            let tagClass = 'crawler-tag pending';
                            if (movie.status === 'failed') {
                                tagClass = 'crawler-tag failed';
                            } else if (cData.status === 'success') {
                                tagClass = 'crawler-tag active';
                            } else if (cData.status === 'failed') {
                                tagClass = 'crawler-tag failed';
                            } else if (cData.status === 'skipped') {
                                tagClass = 'crawler-tag skipped';
                            }
                            tag.className = tagClass;
                            tag.dataset.crawler = cName;
                            tag.textContent = cName;
                            crawlersDiv.appendChild(tag);
                        }
                    }
                }
                updateMovieCount();
            } catch (e) {
                console.error('Failed to restore scrape status:', e);
            }
        }

        let _movieData = {};

        function addMovieItem(dvdid, num, dataSrc, crawlerList) {
            console.log('addMovieItem called:', dvdid, num, dataSrc, crawlerList);
            const container = document.getElementById('movieListContainer');
            console.log('container:', container);
            if (container.querySelector('.movie-detail-empty')) {
                container.innerHTML = '';
            }
            _movieData[dvdid] = { num: num, status: 'scraping', crawlers: {}, save_dir: null, title: '', data_src: dataSrc };

            const item = document.createElement('div');
            item.className = 'movie-item scraping';
            item.id = `movie-${dvdid}`;
            item.onclick = () => selectMovie(dvdid);

            item.innerHTML = `
                <div class="movie-item-header">
                    <span class="movie-item-num">${num}</span>
                    <span class="movie-item-status status-scraping">刮削中</span>
                </div>
                <div class="movie-item-title" id="title-${dvdid}">${dvdid}</div>
                <div class="movie-item-crawlers" id="crawlers-${dvdid}"></div>
            `;
            container.appendChild(item);
            console.log('movie item added');

            // 立即渲染 pending 状态的爬虫标签
            const crawlersDiv = document.getElementById(`crawlers-${dvdid}`);
            if (crawlersDiv) {
                if (crawlerList && crawlerList.length > 0) {
                    crawlerList.forEach(c => {
                        _movieData[dvdid].crawlers[c] = { status: 'pending', fields: [] };
                        const tag = document.createElement('span');
                        tag.className = 'crawler-tag pending';
                        tag.dataset.crawler = c;
                        tag.textContent = c;
                        crawlersDiv.appendChild(tag);
                    });
                } else if (dataSrc) {
                    // 兼容性 fallback：如果后端未传爬虫列表，则请求 API
                    fetch(`/api/crawlers_by_type?type=${dataSrc}`)
                        .then(r => r.json())
                        .then(data => {
                            if (!_movieData[dvdid]) return;
                            // 避免覆盖已有的标签
                            (data.crawlers || []).forEach(c => {
                                if (_movieData[dvdid].crawlers[c]) return;
                                _movieData[dvdid].crawlers[c] = { status: 'pending', fields: [] };
                                const tag = document.createElement('span');
                                tag.className = 'crawler-tag pending';
                                tag.dataset.crawler = c;
                                tag.textContent = c;
                                crawlersDiv.appendChild(tag);
                            });
                        })
                        .catch(err => {
                            console.error('Failed to load crawlers:', err);
                            if (crawlersDiv.children.length === 0) {
                                crawlersDiv.innerHTML = '<span class="inline-error">加载失败</span>';
                            }
                        });
                }
            }
        }

        function updateMovieCrawlers(dvdid, crawler, fields) {
            if (!_movieData[dvdid]) return;
            _movieData[dvdid].crawlers[crawler] = { status: 'success', fields: fields };

            const crawlersDiv = document.getElementById(`crawlers-${dvdid}`);
            if (crawlersDiv) {
                let tag = crawlersDiv.querySelector(`[data-crawler="${crawler}"]`);
                if (!tag) {
                    tag = document.createElement('span');
                    tag.dataset.crawler = crawler;
                    crawlersDiv.appendChild(tag);
                }
                tag.className = 'crawler-tag active';
                tag.textContent = crawler;
            }
        }

        function updateMovieStatus(dvdid, status, saveDir) {
            if (!_movieData[dvdid]) return;
            _movieData[dvdid].status = status;
            if (saveDir) _movieData[dvdid].save_dir = saveDir;

            const item = document.getElementById(`movie-${dvdid}`);
            if (item) {
                item.className = `movie-item ${status}`;
                const statusSpan = item.querySelector('.movie-item-status');
                if (statusSpan) {
                    statusSpan.className = `movie-item-status status-${status}`;
                    statusSpan.textContent = movieStatusLabel(status);
                }
            }

            // 更新爬虫标签颜色：失败时全部标红，成功时未参与的标灰
            const crawlersDiv = document.getElementById(`crawlers-${dvdid}`);
            if (crawlersDiv) {
                if (status === 'failed') {
                    crawlersDiv.querySelectorAll('.crawler-tag').forEach(tag => {
                        if (!tag.classList.contains('failed')) {
                            tag.className = 'crawler-tag failed';
                            const c = tag.dataset.crawler;
                            if (_movieData[dvdid].crawlers[c]) {
                                _movieData[dvdid].crawlers[c].status = 'failed';
                            }
                        }
                    });
                } else if (status === 'success') {
                    crawlersDiv.querySelectorAll('.crawler-tag.pending').forEach(tag => {
                        tag.className = 'crawler-tag skipped';
                        const c = tag.dataset.crawler;
                        if (_movieData[dvdid].crawlers[c]) {
                            _movieData[dvdid].crawlers[c].status = 'skipped';
                        }
                    });
                }
            }
        }

        function updateMovieCount() {
            const completed = Object.values(_movieData).filter(m => m.status === 'success' || m.status === 'failed').length;
            document.getElementById('movieListCount').textContent = `${completed} / ${_totalMovies}`;
        }

        function selectMovie(dvdid) {
            // 更新选中状态
            document.querySelectorAll('.movie-item').forEach(item => item.classList.remove('selected'));
            const item = document.getElementById(`movie-${dvdid}`);
            if (item) item.classList.add('selected');

            const movie = _movieData[dvdid];
            if (!movie) return;
            const crawlerHtml = crawlerDetailHtml(movie);

            if (movie.status !== 'success' || !movie.save_dir) {
                // 显示基本信息
                document.getElementById('movieDetailContent').innerHTML = `
                    <div class="movie-detail-section">
                        <div class="movie-detail-title">${escapeHtml(movie.title || movie.num || dvdid)}</div>
                        <div class="movie-detail-label">状态</div>
                        <div class="movie-detail-value">${movieStatusLabel(movie.status)}</div>
                        <div class="movie-detail-label">完整性</div>
                        <div class="movie-detail-value">${metadataStatusBadge(movie.metadata_status)}</div>
                        ${movie.failure_reason ? `
                            <div class="movie-detail-label">原因</div>
                            <div class="movie-detail-value">${escapeHtml(movie.failure_reason)}</div>
                        ` : ''}
                    </div>
                    ${crawlerHtml}
                `;
                return;
            }

            // 获取影片详情
            fetch(`/api/movie_detail?path=${encodeURIComponent(movie.save_dir)}&num=${encodeURIComponent(movie.num)}`)
                .then(r => r.json())
                .then(detail => {
                    displayMovieDetail(mergeMovieDetailWithTaskInfo(detail, movie), crawlerHtml, movie);
                })
                .catch(err => {
                    document.getElementById('movieDetailContent').innerHTML = `
                        <div class="movie-detail-empty">加载详情失败: ${err}</div>
                    `;
                });
        }

        function displayMovieDetail(detail, crawlerHtml = '', movie = null) {
            const container = document.getElementById('movieDetailContent');
            let html = '';

            if (detail.poster) {
                html += `<img src="${detail.poster}" class="movie-detail-poster" alt="cover">`;
            }

            html += `<div class="movie-detail-title">${detail.title || detail.num}</div>`;

            if (detail.original_title) {
                html += `<div class="movie-detail-section">
                    <div class="movie-detail-label">原始标题</div>
                    <div class="movie-detail-value">${detail.original_title}</div>
                </div>`;
            }

            html += `<div class="movie-detail-section">
                <div class="movie-detail-label">番号</div>
                <div class="movie-detail-value">${detail.num}</div>
            </div>`;

            if (movie) {
                const reasons = movie.metadata_reasons || [];
                html += `<div class="movie-detail-section">
                    <div class="movie-detail-label">完整性</div>
                    <div class="movie-detail-value">${metadataStatusBadge(movie.metadata_status)}</div>
                    ${reasons.length ? `<div class="metadata-reasons">${reasons.map(r => `<span class="metadata-reason-pill">${escapeHtml(r.message || (r.field + ' ' + r.reason))}</span>`).join('')}</div>` : ''}
                    <div class="movie-detail-actions">
                        <button class="task-action-btn" onclick="checkTaskMetadata(${movie.task_id})">重新判断完整性</button>
                        <button class="task-action-btn" onclick="refreshTaskMetadata(${movie.task_id})">优化当前任务</button>
                        ${taskRescrapeActionHtml(movie)}
                    </div>
                </div>`;
            }

            if (detail.actress && detail.actress.length > 0) {
                html += `<div class="movie-detail-section">
                    <div class="movie-detail-label">女演员</div>
                    <div class="movie-detail-actress-list">
                        ${detail.actress.map(a => `<span class="actress-tag">${a}</span>`).join('')}
                    </div>
                </div>`;
            }

            if (detail.genre && detail.genre.length > 0) {
                html += `<div class="movie-detail-section">
                    <div class="movie-detail-label">类型</div>
                    <div class="movie-detail-genre-list">
                        ${detail.genre.map(g => `<span class="genre-tag">${g}</span>`).join('')}
                    </div>
                </div>`;
            }

            if (detail.plot) {
                html += `<div class="movie-detail-section">
                    <div class="movie-detail-label">剧情简介</div>
                    <div class="movie-detail-value plot-text">${detail.plot}</div>
                </div>`;
            }

            if (detail.director) {
                html += `<div class="movie-detail-section">
                    <div class="movie-detail-label">导演</div>
                    <div class="movie-detail-value">${detail.director}</div>
                </div>`;
            }

            if (detail.publisher) {
                html += `<div class="movie-detail-section">
                    <div class="movie-detail-label">发行商</div>
                    <div class="movie-detail-value">${detail.publisher}</div>
                </div>`;
            }

            if (detail.premiered) {
                html += `<div class="movie-detail-section">
                    <div class="movie-detail-label">发行日期</div>
                    <div class="movie-detail-value">${detail.premiered}</div>
                </div>`;
            }

            html += `<div class="movie-detail-section">
                <div class="movie-detail-label">保存路径</div>
                <div class="movie-detail-value path-text">${detail.save_dir}</div>
            </div>`;

            container.innerHTML = html + crawlerHtml;
        }

        function finishRun() {
            runBtn.disabled = false;
            runBtn.textContent = '🚀 启动 JAVSP 主程序';
            if (statusText.textContent === '任务执行中') {
                statusText.textContent = '准备就绪';
            }
            setGlobalStatus('IDLE', 'idle');
            stopTaskCenterPolling();
            refreshMovieListFromTasks({scope: _movieListScope});
            loadTaskDashboard();
        }

        async function loadTaskDashboard() {
            try {
                const [runRes, taskRes, failRes, healthRes, pollRes] = await Promise.all([
                    fetch('/api/runs/latest').then(r => r.json()),
                    fetch('/api/tasks?limit=200').then(r => r.json()),
                    fetch('/api/failures?limit=50').then(r => r.json()),
                    fetch('/api/crawler_health').then(r => r.json()),
                    fetch('/api/polling_active').then(r => r.json())
                ]);

                const run = runRes.run;
                if (run) {
                    const finished = run.finished_at ? new Date(run.finished_at * 1000).toLocaleString() : '运行中';
                    document.getElementById('latestRunSummary').textContent =
                        `${run.status}｜新增 ${run.enqueued || 0}｜成功 ${run.succeeded || 0}｜失败 ${run.failed || 0}｜${finished}`;
                } else {
                    document.getElementById('latestRunSummary').textContent = '尚无记录';
                }

                const counts = {};
                (taskRes.tasks || []).forEach(t => { counts[t.status] = (counts[t.status] || 0) + 1; });
                document.getElementById('taskQueueSummary').textContent =
                    `pending ${counts.pending || 0} / failed ${counts.failed || 0} / deferred ${counts.deferred || 0}`;

                const failures = failRes.tasks || [];
                document.getElementById('failureSummary').textContent = `${failures.length} 个待处理`;

                const crawlers = healthRes.crawlers || [];
                const open = crawlers.filter(c => c.circuit_open).map(c => c.name);
                document.getElementById('crawlerHealthSummary').textContent =
                    open.length ? `熔断: ${open.join(', ')}` : (crawlers.length ? `${crawlers.length} 个爬虫有统计` : '暂无统计');

                const pollData = pollRes || {};
                if (pollData.refresh_starting) {
                    document.getElementById('optimizeSummary').innerHTML =
                        `<span class="summary-warning">⏳ 启动中...</span>`;
                } else if (pollData.refresh_active) {
                    document.getElementById('optimizeSummary').innerHTML =
                        `<span class="summary-accent">运行中：${pollData.refresh_running || 0} 个</span>`;
                } else if (pollData.incomplete_count > 0) {
                    document.getElementById('optimizeSummary').innerHTML =
                        `<span class="summary-warning">待优化：${pollData.incomplete_count} 个</span>
                         <button class="inline-action-btn" onclick="refreshIncompleteMetadata()">⚡ 立即优化</button>`;
                } else if (pollData.not_checked_count > 0) {
                    document.getElementById('optimizeSummary').innerHTML =
                        `<span class="summary-muted">未检查：${pollData.not_checked_count} 个</span>`;
                } else if (pollData.path_unknown_count > 0) {
                    document.getElementById('optimizeSummary').innerHTML =
                        `<span class="summary-muted">路径未知：${pollData.path_unknown_count} 个</span>`;
                } else {
                    document.getElementById('optimizeSummary').innerHTML = '<span class="summary-success">全部完整 ✓</span>';
                }
            } catch (e) {
                console.error('Failed to load task dashboard:', e);
            }
        }

        function escapeHtml(value) {
            return String(value ?? '').replace(/[&<>"']/g, ch => ({
                '&': '&amp;',
                '<': '&lt;',
                '>': '&gt;',
                '"': '&quot;',
                "'": '&#39;'
            }[ch]));
        }

        function statusBadge(status) {
            const safe = escapeHtml(status || 'unknown');
            return `<span class="status-badge ${safe}">${safe}</span>`;
        }

        function metadataStatusBadge(status) {
            const labels = {
                complete: '完整',
                incomplete: '待优化',
                path_unknown: '路径未知',
                not_checked: '未检查',
                '-': '-'
            };
            const klass = status === 'complete' ? 'success' : (status === 'incomplete' || status === 'path_unknown' ? 'failed' : 'pending');
            return `<span class="status-badge ${klass}">${labels[status] || status || '-'}</span>`;
        }

        function rescrapeSessionBadge(session) {
            if (!session) return '';
            const labels = {
                scraping: '候选抓取中',
                awaiting_selection: '待人工选择',
                applying: '正在应用'
            };
            const klass = session.status === 'awaiting_selection' ? 'deferred' : 'running';
            return labels[session.status] ? `<span class="status-badge ${klass}">${labels[session.status]}</span>` : '';
        }

        function taskRescrapeActionHtml(task) {
            const session = task?.rescrape_session;
            if (session?.status === 'awaiting_selection') {
                return `<button class="task-action-btn retry" data-rescrape-session="${session.id}">继续选择</button>`;
            }
            if (session?.status === 'scraping') return '<button class="task-action-btn" disabled>候选抓取中</button>';
            if (session?.status === 'applying') return '<button class="task-action-btn" disabled>正在应用</button>';
            return `<button class="task-action-btn" data-rescrape-task="${task.id || task.task_id}">重新刮削</button>`;
        }

        function formatTime(ts) {
            return ts ? new Date(ts * 1000).toLocaleString() : '-';
        }

        function firstFile(task) {
            const files = task.current_files && task.current_files.length ? task.current_files : (task.files || []);
            return files.length ? files[0] : '';
        }

        function taskPath(task) {
            return firstFile(task) || task.current_nfo_path || task.current_save_dir || task.save_dir || '';
        }

        function taskTypeLabel(task) {
            return task.task_type === 'metadata_legacy' ? '历史导入' : '普通刮削';
        }

        function activityLabel(run) {
            if (run.activity_type === 'metadata_import') return '历史导入';
            if (run.activity_type === 'metadata_refresh') return '元数据优化';
            return run.label || run.trigger || '运行';
        }

        function activitySummary(run) {
            if (run.activity_type === 'metadata_import') {
                return run.message || `扫描 ${run.scan_total || 0}，新增 ${run.enqueued || 0}，更新 ${run.skipped || 0}`;
            }
            if (run.activity_type === 'actress_normalize') {
                const msg = run.message || '';
                const changed = msg.match(/变更\s*(\d+)/)?.[1] || run.succeeded || 0;
                const moved = msg.match(/移动\s*(\d+)/)?.[1] || 0;
                const failed = msg.match(/失败\s*(\d+)/)?.[1] || run.failed || 0;
                return `变更 ${changed}，移动 ${moved}，失败 ${failed}`;
            }
            if (run.activity_type === 'metadata_refresh') {
                return run.message || `${run.label || ''} ${run.trigger ? `(${run.trigger})` : ''}`.trim();
            }
            return run.message || `扫描 ${run.scan_total || 0}，入队 ${run.enqueued || 0}，成功 ${run.succeeded || 0}，失败 ${run.failed || 0}`;
        }

        const TASK_PAGE_SIZE = 12;
        const _taskCenterState = {
            activeSection: 'metadata',
            pages: {queue: 1, metadata: 1, activity: 1, health: 1},
            allTasks: [],
            activities: [],
            crawlers: [],
            latestRun: null,
            selections: {metadata: new Set(), queue: new Set()},
            sortCol: 'updated_at',
            sortDir: 'DESC',
            totalTasks: 0,
            metadataRunFilter: null,
            metadataRunFilterType: 'run_id',
            normalizeActressStartedAt: 0,
            normalizeActressRunning: false,
            normalizeActressCompletedRunId: null,
            polling: {active: false, scraping_active: false, refresh_active: false, refresh_type: null, refresh_running: 0, refresh_starting: false, tasks_pending: 0, tasks_running: 0, incomplete_count: 0, not_checked_count: 0, path_unknown_count: 0, awaiting_selection_count: 0}
        };
        const _detailState = {
            type: null,
            tab: 'overview',
            level: 'all',
            task: null,
            sources: null,
            events: [],
            run: null,
            runTasks: [],
            taskId: null,
            pollTimer: null
        };

        function openDetailShell(kicker, title) {
            document.getElementById('detailKicker').textContent = kicker;
            document.getElementById('detailTitle').textContent = title || '-';
            document.getElementById('detailBody').innerHTML = '<div class="detail-empty">加载中...</div>';
            const overlay = document.getElementById('detailOverlay');
            overlay.classList.add('open');
            overlay.setAttribute('aria-hidden', 'false');
        }

        function closeDetailPanel() {
            stopEventPolling();
            const overlay = document.getElementById('detailOverlay');
            overlay.classList.remove('open');
            overlay.setAttribute('aria-hidden', 'true');
            _detailState.type = null;
        }

        const detailOverlayEl = document.getElementById('detailOverlay');
        if (detailOverlayEl) {
            detailOverlayEl.addEventListener('click', (event) => {
                if (event.target === detailOverlayEl) closeDetailPanel();
            });
        }
        document.addEventListener('keydown', (event) => {
            const overlay = document.getElementById('detailOverlay');
            if (event.key === 'Escape' && overlay?.classList.contains('open')) {
                closeDetailPanel();
            }
        });

        function setDetailTab(tab) {
            _detailState.tab = tab;
            renderDetailPanel();
        }

        function setEventFilter(level) {
            _detailState.level = level;
            renderDetailPanel();
        }

        function stopEventPolling() {
            if (_detailState.pollTimer) {
                clearInterval(_detailState.pollTimer);
                _detailState.pollTimer = null;
            }
        }

        async function pollTaskEvents() {
            if (!_detailState.taskId || _detailState.type !== 'task') {
                stopEventPolling();
                return;
            }
            try {
                const [taskRes, eventsRes] = await Promise.all([
                    fetch(`/api/tasks/${_detailState.taskId}`).then(r => r.ok ? r.json() : Promise.reject(r)),
                    fetch(`/api/tasks/${_detailState.taskId}/events`).then(r => r.ok ? r.json() : Promise.reject(r))
                ]);
                const task = taskRes.task;
                const newEvents = eventsRes.events || [];
                const status = task && task.status;
                _detailState.task = task;
                _detailState.events = newEvents;
                if (_detailState.tab === 'events') {
                    document.getElementById('detailBody').innerHTML = renderEventTimeline(newEvents);
                }
                if (!status || ['success', 'failed', 'skipped'].includes(status)) {
                    stopEventPolling();
                    renderDetailPanel();
                }
            } catch (e) {
                // 网络错误时静默继续轮询
            }
        }

        function startEventPolling() {
            stopEventPolling();
            _detailState.pollTimer = setInterval(pollTaskEvents, 3000);
        }

        async function openTaskDetail(taskId) {
            _detailState.type = 'task';
            _detailState.tab = 'overview';
            _detailState.level = 'all';
            _detailState.task = null;
            _detailState.sources = null;
            _detailState.events = [];
            _detailState.taskId = taskId;
            stopEventPolling();
            openDetailShell('任务详情', `Task #${taskId}`);
            try {
                const [taskRes, sourcesRes, eventsRes] = await Promise.all([
                    fetch(`/api/tasks/${taskId}`).then(r => r.ok ? r.json() : Promise.reject(r)),
                    fetch(`/api/tasks/${taskId}/metadata/sources`).then(r => r.ok ? r.json() : Promise.reject(r)),
                    fetch(`/api/tasks/${taskId}/events`).then(r => r.ok ? r.json() : Promise.reject(r))
                ]);
                _detailState.task = taskRes.task;
                _detailState.sources = sourcesRes;
                _detailState.events = eventsRes.events || [];
                document.getElementById('detailTitle').textContent = `${_detailState.task.avid || '任务'} · #${_detailState.task.id}`;
                if (_detailState.task && ['pending', 'running', 'deferred'].includes(_detailState.task.status)) {
                    startEventPolling();
                }
                renderDetailPanel();
            } catch (e) {
                console.error('Failed to load task detail:', e);
                document.getElementById('detailBody').innerHTML = '<div class="detail-empty">任务详情加载失败</div>';
                showToast('任务详情加载失败', 'error');
            }
        }

        async function openRunDetail(runId) {
            _detailState.type = 'run';
            _detailState.tab = 'overview';
            _detailState.level = 'all';
            _detailState.run = null;
            _detailState.runTasks = [];
            _detailState.events = [];
            _detailState.taskId = null;
            stopEventPolling();
            openDetailShell('运行详情', `Run #${runId}`);
            try {
                const data = await fetch(`/api/runs/${runId}`).then(r => r.ok ? r.json() : Promise.reject(r));
                _detailState.run = data.run;
                _detailState.runTasks = data.tasks || [];
                _detailState.events = data.events || [];
                document.getElementById('detailTitle').textContent = `${activityLabel({...data.run, activity_type: data.run.trigger === 'metadata-import' ? 'metadata_import' : 'scrape_run'})} · #${data.run.id}`;
                renderDetailPanel();
            } catch (e) {
                console.error('Failed to load run detail:', e);
                document.getElementById('detailBody').innerHTML = '<div class="detail-empty">运行详情加载失败</div>';
                showToast('运行详情加载失败', 'error');
            }
        }

        function renderDetailPanel() {
            const tabs = _detailState.type === 'run'
                ? [{id: 'overview', label: '概览'}, {id: 'events', label: '运行日志'}]
                : [{id: 'overview', label: '概览'}, {id: 'sources', label: '来源对比'}, {id: 'events', label: '任务日志'}];
            document.getElementById('detailTabs').innerHTML = tabs.map(tab => `
                <button class="detail-tab ${_detailState.tab === tab.id ? 'active' : ''}" onclick="setDetailTab('${tab.id}')">${tab.label}</button>
            `).join('');
            if (_detailState.type === 'run') {
                document.getElementById('detailBody').innerHTML = _detailState.tab === 'events' ? renderEventTimeline(_detailState.events) : renderRunOverview();
                return;
            }
            if (!_detailState.task) {
                document.getElementById('detailBody').innerHTML = '<div class="detail-empty">加载中...</div>';
                return;
            }
            if (_detailState.tab === 'sources') {
                document.getElementById('detailBody').innerHTML = renderSourceComparison();
            } else if (_detailState.tab === 'events') {
                document.getElementById('detailBody').innerHTML = renderEventTimeline(_detailState.events);
            } else {
                document.getElementById('detailBody').innerHTML = renderTaskOverview();
            }
        }

        function renderTaskOverview() {
            const task = _detailState.task || {};
            const files = task.current_files && task.current_files.length ? task.current_files : (task.files || []);
            const info = task.info || {};
            const crawlerRows = (task.crawler_results || []).map(result => `
                <div class="source-item">
                    <span class="source-name">${escapeHtml(result.crawler_name)}</span>
                    <span>${result.success ? '<span class="source-chip">成功</span>' : '<span class="source-chip error">失败</span>'}
                    ${escapeHtml(result.title || result.error || (result.fields || []).join(', ') || '-')}</span>
                </div>
            `).join('');
            return `
                <div class="detail-grid">
                    <div class="detail-stat"><div class="detail-stat-label">状态</div><div class="detail-stat-value">${statusBadge(task.status)}</div></div>
                    <div class="detail-stat"><div class="detail-stat-label">元数据</div><div class="detail-stat-value">${metadataStatusBadge(task.metadata_status)}</div></div>
                    <div class="detail-stat"><div class="detail-stat-label">更新时间</div><div class="detail-stat-value">${escapeHtml(formatTime(task.updated_at))}</div></div>
                </div>
                <div class="detail-section">
                    <h3>${escapeHtml(info.title || task.avid || '-')}</h3>
                    <div class="detail-path">${escapeHtml(info.plot || task.failure_reason || '暂无简介')}</div>
                </div>
                <div class="detail-section">
                    <h3>文件位置</h3>
                    <div class="detail-path">${files.length ? files.map(escapeHtml).join('<br>') : '-'}</div>
                    <div class="detail-path">NFO：${escapeHtml(task.current_nfo_path || '-')}</div>
                    <div class="detail-path">目录：${escapeHtml(task.current_save_dir || task.save_dir || '-')}</div>
                </div>
                <div class="detail-section">
                    <h3>爬虫结果</h3>
                    <div class="source-list">${crawlerRows || '<div class="detail-path">暂无爬虫结果</div>'}</div>
                </div>
            `;
        }

        function renderRunOverview() {
            const run = _detailState.run || {};
            const tasks = _detailState.runTasks || [];
            const rows = tasks.map(task => `
                <tr>
                    <td>${task.id}</td>
                    <td>${escapeHtml(task.avid)}</td>
                    <td>${statusBadge(task.status)}</td>
                    <td>${metadataStatusBadge(task.metadata_status)}</td>
                    <td><button class="task-action-btn" onclick="openTaskDetail(${task.id})">详情</button></td>
                </tr>
            `).join('');
            return `
                <div class="detail-grid">
                    <div class="detail-stat"><div class="detail-stat-label">状态</div><div class="detail-stat-value">${statusBadge(run.status)}</div></div>
                    <div class="detail-stat"><div class="detail-stat-label">开始时间</div><div class="detail-stat-value">${escapeHtml(formatTime(run.started_at))}</div></div>
                    <div class="detail-stat"><div class="detail-stat-label">结束时间</div><div class="detail-stat-value">${escapeHtml(formatTime(run.finished_at))}</div></div>
                </div>
                <div class="detail-section">
                    <h3>运行摘要</h3>
                    <div class="source-list">
                        <div class="source-item"><span class="source-name">扫描</span><span>${run.scan_total || 0}</span></div>
                        <div class="source-item"><span class="source-name">入队</span><span>${run.enqueued || 0}</span></div>
                        <div class="source-item"><span class="source-name">延迟 / 跳过</span><span>${run.deferred || 0} / ${run.skipped || 0}</span></div>
                        <div class="source-item"><span class="source-name">成功 / 失败</span><span>${run.succeeded || 0} / ${run.failed || 0}</span></div>
                        ${run.message ? `<div class="source-item"><span class="source-name">消息</span><span>${escapeHtml(run.message)}</span></div>` : ''}
                    </div>
                </div>
                <div class="source-table-wrap">
                    <table class="source-table">
                        <thead><tr><th>ID</th><th>番号</th><th>状态</th><th>完整性</th><th>操作</th></tr></thead>
                        <tbody>${rows || '<tr><td colspan="5" class="task-muted">本次运行暂无关联任务</td></tr>'}</tbody>
                    </table>
                </div>
            `;
        }

        function sourceStatusChip(field) {
            if (field.status === 'missing') return '<span class="source-chip warning">缺失</span>';
            if (field.status === 'conflict') return '<span class="source-chip warning">冲突</span>';
            if (field.status === 'matched') return '<span class="source-chip">已匹配</span>';
            return '<span class="source-chip warning">未知</span>';
        }

        function formatSourceValue(value) {
            if (value === null || value === undefined || value === '' || (Array.isArray(value) && value.length === 0)) {
                return '<span class="source-value empty">空</span>';
            }
            if (Array.isArray(value)) {
                return `<div class="source-value">${value.map(v => `<span class="source-chip">${escapeHtml(String(v))}</span>`).join('')}</div>`;
            }
            if (typeof value === 'object') {
                return `<div class="source-value">${escapeHtml(JSON.stringify(value))}</div>`;
            }
            const text = String(value);
            if (text.startsWith('http://') || text.startsWith('https://')) {
                return `<a class="source-value" href="${escapeHtml(text)}" target="_blank">${escapeHtml(text)}</a>`;
            }
            return `<div class="source-value">${escapeHtml(text)}</div>`;
        }

        function renderSourceComparison() {
            const sources = _detailState.sources || {};
            const rows = (sources.fields || []).map(field => `
                <tr>
                    <td><strong>${escapeHtml(field.label || field.field)}</strong><div class="task-muted">${escapeHtml(field.field)}</div></td>
                    <td>${formatSourceValue(field.final_value)}</td>
                    <td>${sourceStatusChip(field)}<div class="task-muted">${escapeHtml(field.adopted_source || '-')}</div></td>
                    <td>
                        <div class="source-list">
                            ${(field.sources || []).map(source => `
                                <div class="source-item">
                                    <span class="source-name">${escapeHtml(source.crawler_name || '-')}</span>
                                    <span>${source.success ? formatSourceValue(source.value) : `<span class="source-chip error">失败</span> ${escapeHtml(source.error || '')}`}</span>
                                </div>
                            `).join('')}
                        </div>
                    </td>
                </tr>
            `).join('');
            return `
                ${sources.note ? `<div class="source-note">${escapeHtml(sources.note)}</div>` : ''}
                <div class="source-table-wrap">
                    <table class="source-table">
                        <thead><tr><th>字段</th><th>最终值</th><th>采用来源</th><th>各爬虫返回值</th></tr></thead>
                        <tbody>${rows || '<tr><td colspan="4" class="task-muted">暂无来源记录</td></tr>'}</tbody>
                    </table>
                </div>
            `;
        }

        function renderEventTimeline(events) {
            const filters = [
                ['all', '全部'],
                ['info', '信息'],
                ['warning', '警告'],
                ['error', '错误']
            ];
            const filtered = (_detailState.level === 'all' ? events : events.filter(event => event.level === _detailState.level)) || [];
            const items = filtered.map(event => {
                const payload = event.payload && Object.keys(event.payload).length ? JSON.stringify(event.payload, null, 2) : '';
                const clipped = payload.length > 900 ? payload.slice(0, 900) + '\n...' : payload;
                return `
                    <div class="event-item ${escapeHtml(event.level || 'info')}">
                        <div class="event-line">
                            <div class="event-title">${escapeHtml(event.message || event.event_type)}</div>
                            <div class="event-time">${escapeHtml(formatTime(event.created_at))}</div>
                        </div>
                        <div class="task-muted">${escapeHtml(event.event_type || '')}${event.task_id ? ` · Task #${event.task_id}` : ''}</div>
                        ${clipped ? `<div class="event-meta">${escapeHtml(clipped)}</div>` : ''}
                    </div>
                `;
            }).join('');
            return `
                <div class="event-toolbar">
                    ${filters.map(([id, label]) => `<button class="event-filter ${_detailState.level === id ? 'active' : ''}" onclick="setEventFilter('${id}')">${label}</button>`).join('')}
                </div>
                <div class="event-list">${items || '<div class="detail-empty">暂无结构化日志</div>'}</div>
            `;
        }

        function setSort(col) {
            if (_taskCenterState.sortCol === col) {
                _taskCenterState.sortDir = _taskCenterState.sortDir === 'DESC' ? 'ASC' : 'DESC';
            } else {
                _taskCenterState.sortCol = col;
                _taskCenterState.sortDir = 'DESC';
            }
            document.querySelectorAll('.sort-arrow').forEach(a => a.textContent = '');
            const arrow = document.querySelector(`.sort-arrow[data-col="${col}"]`);
            if (arrow) arrow.textContent = _taskCenterState.sortDir === 'DESC' ? ' ▼' : ' ▲';
            _taskCenterState.pages.metadata = 1;
            _taskCenterState.pages.queue = 1;
            renderTaskCenter();
        }

        function selectAllTasks(section) {
            const sel = _taskCenterState.selections[section];
            const allTasks = _taskCenterState.allTasks || [];
            let tasks;
            if (section === 'metadata') {
                tasks = allTasks.filter(t => t.status === 'success').filter(t => {
                    const mf = document.getElementById('taskFilterMetadata')?.value || '';
                    const st = (document.getElementById('taskFilterSearch')?.value || '').trim().toLowerCase();
                    if (mf && t.metadata_status !== mf) return false;
                    if (st) {
                        const hs = `${t.avid || ''} ${taskPath(t)} ${t.current_save_dir || ''}`.toLowerCase();
                        if (!hs.includes(st)) return false;
                    }
                    return true;
                });
            } else if (section === 'queue') {
                tasks = allTasks.filter(t => ['pending', 'running', 'deferred', 'failed'].includes(t.status));
            } else return;
            const page = pageItems(section, tasks);
            page.items.forEach(t => sel.add(t.id));
            renderTaskCenter();
        }

        function clearAllSelections(section) {
            _taskCenterState.selections[section] = new Set();
            renderTaskCenter();
        }

        function toggleSelectAll(section, checked) {
            if (checked) selectAllTasks(section);
            else clearAllSelections(section);
        }

        function isTaskSelected(section, id) {
            return _taskCenterState.selections[section]?.has(id) || false;
        }

        function toggleTaskSelection(section, id) {
            const sel = _taskCenterState.selections[section];
            if (sel.has(id)) sel.delete(id); else sel.add(id);
            renderTaskCenter();
        }

        async function batchDeleteSelected(section) {
            const ids = Array.from(_taskCenterState.selections[section] || []);
            if (!ids.length) { showToast('请先选择任务', 'error'); return; }
            if (!confirm(`即将删除 ${ids.length} 条任务记录（不删除影片文件），确定？`)) return;
            try {
                const res = await fetch('/api/tasks/batch-delete', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({task_ids: ids})
                });
                const data = await res.json();
                showToast(data.message, res.ok ? 'success' : 'error');
                _taskCenterState.selections[section] = new Set();
                loadTaskCenter();
                refreshMovieListFromTasks({scope: _movieListScope});
                loadTaskDashboard();
            } catch(e) { showToast('网络错误', 'error'); }
        }

        async function batchRetrySelected() {
            const ids = Array.from(_taskCenterState.selections.queue || []);
            if (!ids.length) { showToast('请先选择任务', 'error'); return; }
            try {
                const res = await fetch('/api/tasks/batch-retry', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({task_ids: ids})
                });
                const data = await res.json();
                showToast(data.message, res.ok ? 'success' : 'error');
                _taskCenterState.selections.queue = new Set();
                loadTaskCenter();
                loadTaskDashboard();
            } catch(e) { showToast('网络错误', 'error'); }
        }

        async function batchDeleteByStatus(status) {
            const labels = {success: '成功', failed: '失败', skipped: '跳过'};
            const label = labels[status] || status;
            if (!confirm(`即将删除所有状态为"${label}"的任务记录（不删除影片文件），确定？`)) return;
            try {
                const res = await fetch('/api/tasks/batch-delete', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({status: status})
                });
                const data = await res.json();
                showToast(data.message, res.ok ? 'success' : 'error');
                loadTaskCenter();
                refreshMovieListFromTasks({scope: _movieListScope});
                loadTaskDashboard();
            } catch(e) { showToast('网络错误', 'error'); }
        }

        async function batchDeleteOldRecords(status, days) {
            if (!confirm(`即将删除 ${days} 天前的所有"成功"任务记录（不删除影片文件），确定？`)) return;
            try {
                const res = await fetch('/api/tasks/batch-delete', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({status: status, older_than_days: days})
                });
                const data = await res.json();
                showToast(data.message, res.ok ? 'success' : 'error');
                loadTaskCenter();
                refreshMovieListFromTasks({scope: _movieListScope});
                loadTaskDashboard();
            } catch(e) { showToast('网络错误', 'error'); }
        }

        function exportTasksCSV() {
            window.open('/api/tasks/export', '_blank');
        }

        function switchTaskSection(section) {
            _taskCenterState.activeSection = section;
            if (section !== 'metadata') {
                _taskCenterState.metadataRunFilter = null;
                _taskCenterState.metadataRunFilterType = 'run_id';
            }
            document.querySelectorAll('.task-section-tab').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.taskSection === section);
            });
            document.querySelectorAll('.task-section-panel').forEach(panel => panel.classList.remove('active'));
            const panel = document.getElementById(`taskSection${section.charAt(0).toUpperCase()}${section.slice(1)}`);
            if (panel) panel.classList.add('active');
            renderTaskCenter();
        }

        function resetTaskPage(section) {
            _taskCenterState.pages[section] = 1;
        }

        function setTaskPage(section, page) {
            _taskCenterState.pages[section] = page;
            renderTaskCenter();
        }

        function pageItems(section, items) {
            const totalPages = Math.max(1, Math.ceil(items.length / TASK_PAGE_SIZE));
            const page = Math.min(Math.max(1, _taskCenterState.pages[section] || 1), totalPages);
            _taskCenterState.pages[section] = page;
            const start = (page - 1) * TASK_PAGE_SIZE;
            return {items: items.slice(start, start + TASK_PAGE_SIZE), page, totalPages};
        }

        function renderPagination(section, total) {
            const el = document.getElementById(`${section}Pagination`);
            if (!el) return;
            const totalPages = Math.max(1, Math.ceil(total / TASK_PAGE_SIZE));
            const page = Math.min(Math.max(1, _taskCenterState.pages[section] || 1), totalPages);
            const start = total ? (page - 1) * TASK_PAGE_SIZE + 1 : 0;
            const end = Math.min(page * TASK_PAGE_SIZE, total);
            el.innerHTML = `
                <span>${start}-${end} / ${total} 条</span>
                <div class="task-pagination-controls">
                    <button class="task-page-btn" onclick="setTaskPage('${section}', ${page - 1})" ${page <= 1 ? 'disabled' : ''}>上一页</button>
                    <span>${page} / ${totalPages}</span>
                    <button class="task-page-btn" onclick="setTaskPage('${section}', ${page + 1})" ${page >= totalPages ? 'disabled' : ''}>下一页</button>
                </div>
            `;
        }

        function mergeMovieDetailWithTaskInfo(detail, movie) {
            const info = movie?.info || {};
            return {
                ...detail,
                num: info.dvdid || info.cid || detail.num || movie?.num || '',
                title: info.nfo_title || info.title || detail.title || '',
                original_title: info.ori_title || detail.original_title || '',
                plot: info.plot || detail.plot || '',
                actress: (info.actress && info.actress.length) ? info.actress : (detail.actress || []),
                director: info.director || detail.director || '',
                publisher: info.publisher || detail.publisher || '',
                producer: info.producer || detail.producer || '',
                genre: (info.genre && info.genre.length) ? info.genre : (detail.genre || []),
                premiered: info.publish_date || detail.premiered || ''
            };
        }

        function movieStatusFromTask(status) {
            if (status === 'running') return 'scraping';
            return status || 'pending';
        }

        function movieStatusLabel(status) {
            const labels = {
                success: '成功',
                failed: '失败',
                scraping: '刮削中',
                running: '刮削中',
                pending: '等待中',
                deferred: '延迟',
                skipped: '跳过'
            };
            return labels[status] || status || '未知';
        }

        function crawlerStatusLabel(status) {
            const labels = {
                success: '成功',
                failed: '失败',
                pending: '等待中',
                skipped: '未参与'
            };
            return labels[status] || status || '未知';
        }

        function taskToMovieData(task) {
            const results = {};
            (task.crawler_results || []).forEach(result => {
                results[result.crawler_name] = result;
            });
            const crawlerNames = Array.from(new Set([
                ...(task.configured_crawlers || []),
                ...Object.keys(results)
            ]));
            const crawlers = {};
            crawlerNames.forEach(name => {
                const result = results[name];
                if (result) {
                    crawlers[name] = {
                        status: result.success ? 'success' : 'failed',
                        fields: result.fields || [],
                        title: result.title || '',
                        url: result.url || ''
                    };
                } else {
                    crawlers[name] = {
                        status: task.status === 'success' || task.status === 'failed' ? 'skipped' : 'pending',
                        fields: []
                    };
                }
            });
            const info = task.info || null;
            const titleResult = Object.values(results).find(result => result.title);
            return {
                task_id: task.id,
                num: task.avid,
                status: movieStatusFromTask(task.status),
                task_status: task.status,
                crawlers,
                save_dir: task.current_save_dir || task.save_dir,
                title: info ? (info.nfo_title || info.title || '') : (titleResult ? titleResult.title : ''),
                info,
                data_src: task.data_src || 'normal',
                files: task.files || [],
                failure_stage: task.failure_stage || '',
                failure_reason: task.failure_reason || '',
                metadata_status: task.metadata_status || '-',
                metadata_reasons: task.metadata_incomplete_reasons || [],
                metadata_checked_at: task.metadata_checked_at,
                last_metadata_refresh_at: task.last_metadata_refresh_at,
                rescrape_session: task.rescrape_session || null
            };
        }

        function crawlerDetailHtml(movie) {
            const crawlers = movie.crawlers || {};
            const names = Object.keys(crawlers);
            if (!names.length) {
                return `
                    <div class="movie-detail-section">
                        <div class="movie-detail-label">爬虫过程</div>
                        <div class="movie-detail-value">暂无爬虫记录</div>
                    </div>
                `;
            }
            return `
                <div class="movie-detail-section">
                    <div class="movie-detail-label">爬虫过程</div>
                    <div class="crawler-detail-list">
                        ${names.map(name => {
                            const crawler = crawlers[name] || {};
                            const status = crawler.status || 'pending';
                            const badgeStatus = status === 'success' ? 'success' : (status === 'failed' ? 'failed' : (status === 'skipped' ? 'skipped' : 'pending'));
                            const fields = (crawler.fields || []).length ? crawler.fields.join(', ') : '暂无字段';
                            return `
                                <div class="crawler-detail-item">
                                    <div class="crawler-detail-line">
                                        <span class="crawler-detail-name">${escapeHtml(name)}</span>
                                        <span class="status-badge ${badgeStatus}">${crawlerStatusLabel(status)}</span>
                                    </div>
                                    <div class="crawler-detail-fields">${escapeHtml(fields)}</div>
                                    ${crawler.title ? `<div class="crawler-detail-fields">标题：${escapeHtml(crawler.title)}</div>` : ''}
                                    ${crawler.url ? `<div class="crawler-detail-fields">来源：${escapeHtml(crawler.url)}</div>` : ''}
                                </div>
                            `;
                        }).join('')}
                    </div>
                </div>
            `;
        }

        function startTaskPolling() {
            ensureTaskCenterPolling();
        }

        let _taskCenterPollTimer = null;

        function ensureTaskCenterPolling() {
            if (_taskCenterPollTimer) return;
            const poll = () => {
                Promise.all([
                    fetch('/api/polling_active').then(r => r.json()),
                    fetch('/api/runs/latest').then(r => r.json()),
                    fetch('/api/runs/activity?limit=20').then(r => r.json())
                ]).then(([activeData, runData, activityData]) => {
                    if (activeData) {
                        _taskCenterState.polling = activeData;
                    }
                    if (activityData && activityData.runs) {
                        _taskCenterState.activities = activityData.runs;
                        updateNormalizeActressStatus(activityData.runs, activeData);
                    }
                    const active = (activeData && activeData.active)
                        || (runData.run && runData.run.status === 'running');
                    if (active) {
                        loadTaskCenter();
                        loadTaskDashboard();
                        refreshMovieListFromTasks({scope: _movieListScope});
                        _taskCenterPollTimer = setTimeout(poll, 5000);
                    } else {
                        _taskCenterPollTimer = null;
                    }
                }).catch(() => {
                    _taskCenterPollTimer = setTimeout(poll, 5000);
                });
            };
            _taskCenterPollTimer = setTimeout(poll, 1000);
        }

        function setNormalizeActressRunning(isRunning) {
            _taskCenterState.normalizeActressRunning = isRunning;
            const btn = document.getElementById('normalizeActressBtn');
            if (btn) btn.disabled = isRunning;
        }

        function updateNormalizeActressStatus(activities, polling) {
            const summaryEl = document.getElementById('normalizeActressSummary');
            if (!summaryEl) return;
            const p = polling || _taskCenterState.polling || {};
            if (p.refresh_type === 'normalize-actress' && p.refresh_active) {
                setNormalizeActressRunning(true);
                summaryEl.textContent = '归一化运行中...';
                return;
            }
            const startedAt = _taskCenterState.normalizeActressStartedAt || 0;
            const latest = (activities || [])
                .filter(run => run.activity_type === 'actress_normalize')
                .filter(run => !startedAt || Number(run.started_at || 0) >= startedAt)
                .sort((a, b) => Number(b.started_at || 0) - Number(a.started_at || 0))[0];
            if (!latest || !latest.finished_at) return;
            if (_taskCenterState.normalizeActressCompletedRunId === latest.id) return;
            _taskCenterState.normalizeActressCompletedRunId = latest.id;
            setNormalizeActressRunning(false);
            const prefix = latest.status === 'success' ? '完成' : '失败';
            summaryEl.textContent = `${prefix}：${activitySummary(latest)}`;
        }

        function stopTaskCenterPolling() {
            if (_taskCenterPollTimer) {
                clearTimeout(_taskCenterPollTimer);
                _taskCenterPollTimer = null;
            }
        }

        function ensureTaskPolling() {
            stopTaskCenterPolling();
            ensureTaskCenterPolling();
        }

        async function refreshMovieListFromTasks({scope = _movieListScope} = {}) {
            try {
                const params = new URLSearchParams({limit: '300', include_results: '1'});
                let run = null;
                if (scope === 'latest') {
                    const runRes = await fetch('/api/runs/latest').then(r => r.json());
                    run = runRes.run;
                    if (run) params.set('last_run_id', String(run.id));
                } else if (scope === 'recent10') {
                    params.set('limit', '10');
                } else if (scope === 'day1' || scope === 'day7') {
                    const days = scope === 'day1' ? 1 : 7;
                    params.set('updated_after', String(Date.now() / 1000 - days * 86400));
                } else if (scope === 'success' || scope === 'failed') {
                    params.set('status', scope);
                }
                const taskRes = run || scope !== 'latest'
                    ? await fetch(`/api/tasks?${params.toString()}`).then(r => r.json())
                    : {tasks: []};
                let tasks = taskRes.tasks || [];
                if (scope === 'latest' && run) {
                    if (_runStartedAfter && Number(run.started_at || 0) < _runStartedAfter) return;
                }

                tasks.sort((a, b) => Number(a.id) - Number(b.id));
                const selectedKey = document.querySelector('.movie-item.selected')?.dataset.movieKey;
                const container = document.getElementById('movieListContainer');
                _movieData = {};
                tasks.forEach(task => {
                    _movieData[`task-${task.id}`] = taskToMovieData(task);
                });
                _totalMovies = tasks.length;
                if (!tasks.length) {
                    container.innerHTML = `<div class="movie-detail-empty">${movieListEmptyText(scope)}</div>`;
                    document.getElementById('movieDetailContent').innerHTML = '<div class="movie-detail-empty">点击左侧列表中的影片查看详情</div>';
                    updateMovieCount();
                    return;
                }
                container.innerHTML = tasks.map(task => {
                    const key = `task-${task.id}`;
                    const movie = _movieData[key];
                    const status = movie.status;
                    const title = movie.title || movie.num || key;
                    return `
                        <div class="movie-item ${status}" id="movie-${key}" data-movie-key="${key}">
                            <div class="movie-item-header">
                                <span class="movie-item-num">${escapeHtml(movie.num || key)}</span>
                                <span class="movie-item-status status-${status}">${movieStatusLabel(status)}</span>
                            </div>
                            <div class="movie-item-title" id="title-${key}">${escapeHtml(title)}</div>
                            <div class="movie-item-title">${metadataStatusBadge(movie.metadata_status)}</div>
                            <div class="movie-item-crawlers" id="crawlers-${key}">
                                ${Object.entries(movie.crawlers || {}).map(([name, crawler]) => {
                                    const tagClass = crawler.status === 'success' ? 'active' : crawler.status;
                                    return `<span class="crawler-tag ${tagClass}" data-crawler="${escapeHtml(name)}">${escapeHtml(name)}</span>`;
                                }).join('')}
                            </div>
                        </div>
                    `;
                }).join('');
                container.querySelectorAll('.movie-item').forEach(item => {
                    item.onclick = () => selectMovie(item.dataset.movieKey);
                });
                if (selectedKey && _movieData[selectedKey]) {
                    selectMovie(selectedKey);
                }
                updateMovieCount();
            } catch (e) {
                console.error('Failed to refresh movie list from tasks:', e);
            }
        }

        function movieListEmptyText(scope) {
            const labels = {
                latest: '最近运行暂无影片任务',
                recent10: '暂无影片任务',
                day1: '过去 1 天暂无影片任务',
                day7: '过去 7 天暂无影片任务',
                all: '状态库暂无影片任务',
                success: '暂无成功影片',
                failed: '暂无失败影片'
            };
            return labels[scope] || '暂无影片任务';
        }

        async function retryTask(taskId) {
            try {
                const res = await fetch(`/api/tasks/${taskId}/retry`, {method: 'POST'});
                const data = await res.json();
                if (res.ok) {
                    showToast(data.message || '任务已重新加入队列');
                    loadTaskCenter();
                    loadTaskDashboard();
                    if (_detailState.type === 'task' && _detailState.taskId === taskId) {
                        startEventPolling();
                        pollTaskEvents();
                    }
                } else {
                    showToast(data.message || '重试失败', 'error');
                }
            } catch (e) {
                showToast('网络错误，请稍后重试', 'error');
            }
        }

        async function deleteTaskRecord(taskId) {
            try {
                const res = await fetch(`/api/tasks/${taskId}`, {method: 'DELETE'});
                const data = await res.json();
                showToast(data.message || (res.ok ? '任务记录已删除' : '删除失败'), res.ok ? 'success' : 'error');
                loadTaskCenter();
                refreshMovieListFromTasks({scope: _movieListScope});
                loadTaskDashboard();
            } catch (e) {
                showToast('网络错误，请稍后重试', 'error');
            }
        }

        async function checkTaskMetadata(taskId) {
            try {
                const res = await fetch(`/api/tasks/${taskId}/metadata/check`, {method: 'POST'});
                const data = await res.json();
                showToast(data.message || (res.ok ? '完整性已重新判断' : '检查失败'), res.ok ? 'success' : 'error');
                loadTaskCenter();
                refreshMovieListFromTasks({scope: _movieListScope});
            } catch (e) {
                showToast('网络错误，请稍后重试', 'error');
            }
        }

        async function refreshTaskMetadata(taskId) {
            try {
                const res = await fetch(`/api/tasks/${taskId}/metadata/refresh`, {method: 'POST'});
                const data = await res.json();
                showToast(data.message || (res.ok ? '优化任务已启动' : '优化启动失败'), res.ok ? 'success' : 'error');
                loadTaskCenter();
                ensureTaskPolling();
            } catch (e) {
                showToast('网络错误，请稍后重试', 'error');
            }
        }

        const _rescrapeState = {
            taskId: null,
            sessionId: null,
            data: null,
            pollTimer: null,
            autoOpened: new Set()
        };
        document.addEventListener('click', (event) => {
            const target = event.target instanceof Element ? event.target : null;
            const taskButton = target?.closest('[data-rescrape-task]');
            if (taskButton) {
                rescrapeTaskMetadata(Number(taskButton.dataset.rescrapeTask));
                return;
            }
            const sessionButton = target?.closest('[data-rescrape-session]');
            if (sessionButton) {
                continueInteractiveRescrape(Number(sessionButton.dataset.rescrapeSession));
                return;
            }
            const candidateButton = target?.closest('[data-rescrape-field][data-rescrape-origin]');
            if (candidateButton) {
                const sourceIndex = candidateButton.dataset.rescrapeIndex;
                selectRescrapeValue(
                    candidateButton.dataset.rescrapeField,
                    candidateButton.dataset.rescrapeOrigin,
                    sourceIndex === undefined ? undefined : Number(sourceIndex)
                );
                return;
            }
            const clearButton = target?.closest('[data-rescrape-clear]');
            if (clearButton) clearRescrapeField(clearButton.dataset.rescrapeClear);
        });
        document.addEventListener('input', (event) => {
            const input = event.target instanceof Element ? event.target.closest('[data-rescrape-input]') : null;
            if (input) markRescrapeManual(input.dataset.rescrapeInput);
        });

        function rescrapeTaskMetadata(taskId) {
            const task = (_taskCenterState.allTasks || []).find(item => Number(item.id) === Number(taskId));
            const session = task?.rescrape_session;
            if (session?.status === 'awaiting_selection') {
                continueInteractiveRescrape(session.id);
                return;
            }
            if (session?.status === 'scraping' || session?.status === 'applying') {
                showToast(session.status === 'scraping' ? '候选数据仍在抓取中' : '最终结果正在应用中');
                pollInteractiveRescrape(session.id, true);
                return;
            }
            _rescrapeState.taskId = taskId;
            const overlay = document.getElementById('rescrapeModeOverlay');
            overlay.classList.add('open');
            overlay.setAttribute('aria-hidden', 'false');
        }

        function closeRescrapeMode() {
            const overlay = document.getElementById('rescrapeModeOverlay');
            overlay.classList.remove('open');
            overlay.setAttribute('aria-hidden', 'true');
        }

        async function startRescrapeMode(mode) {
            const taskId = _rescrapeState.taskId;
            if (!taskId) return;
            closeRescrapeMode();
            if (mode === 'auto') {
                if (!confirm('将按原有规则重新爬取并替换旧元数据、NFO 和媒体文件。不会移动影片文件。确定继续？')) return;
                try {
                    // 保持原有 API 和请求格式不变。
                    const res = await fetch(`/api/tasks/${taskId}/metadata/rescrape`, {method: 'POST'});
                    const data = await res.json();
                    showToast(data.message || (res.ok ? '重新刮削任务已启动' : '重新刮削启动失败'), res.ok ? 'success' : 'error');
                    loadTaskCenter();
                    ensureTaskPolling();
                } catch (e) {
                    showToast('网络错误，请稍后重试', 'error');
                }
                return;
            }

            try {
                const res = await fetch(`/api/tasks/${taskId}/metadata/rescrape/interactive`, {method: 'POST'});
                const data = await res.json();
                if (!res.ok) {
                    showToast(data.message || '自由选择模式启动失败', 'error');
                    return;
                }
                const session = data.session;
                _rescrapeState.sessionId = session.id;
                showToast(data.message || '候选数据抓取已启动');
                if (session.status === 'awaiting_selection') {
                    continueInteractiveRescrape(session.id);
                } else {
                    pollInteractiveRescrape(session.id, true);
                }
                loadTaskCenter();
                ensureTaskPolling();
            } catch (e) {
                showToast('网络错误，请稍后重试', 'error');
            }
        }

        function stopRescrapePolling() {
            if (_rescrapeState.pollTimer) {
                clearTimeout(_rescrapeState.pollTimer);
                _rescrapeState.pollTimer = null;
            }
        }

        async function pollInteractiveRescrape(sessionId, notifyWhenWaiting = false) {
            stopRescrapePolling();
            try {
                const res = await fetch(`/api/rescrape-sessions/${sessionId}`);
                const data = await res.json();
                if (!res.ok) throw new Error(data.message || '会话加载失败');
                const status = data.session.status;
                if (status === 'awaiting_selection') {
                    if (notifyWhenWaiting) showToast('候选抓取完成，请选择最终字段');
                    openRescrapeEditorData(data);
                    loadTaskCenter();
                    return;
                }
                if (status === 'completed') {
                    showToast('自由选择重新刮削已完成');
                    closeRescrapeEditor();
                    loadTaskCenter();
                    refreshMovieListFromTasks({scope: _movieListScope});
                    return;
                }
                if (status === 'failed' || status === 'cancelled') {
                    showToast(data.session.error || (status === 'cancelled' ? '本次重新刮削已取消' : '自由选择重新刮削失败'), status === 'cancelled' ? 'success' : 'error');
                    loadTaskCenter();
                    return;
                }
                _rescrapeState.pollTimer = setTimeout(() => pollInteractiveRescrape(sessionId, true), 2000);
            } catch (e) {
                _rescrapeState.pollTimer = setTimeout(() => pollInteractiveRescrape(sessionId, notifyWhenWaiting), 3000);
            }
        }

        async function continueInteractiveRescrape(sessionId) {
            stopRescrapePolling();
            _rescrapeState.sessionId = sessionId;
            try {
                const res = await fetch(`/api/rescrape-sessions/${sessionId}`);
                const data = await res.json();
                if (!res.ok) throw new Error(data.message || '会话加载失败');
                if (data.session.status === 'awaiting_selection') {
                    openRescrapeEditorData(data);
                } else {
                    showToast(data.session.status === 'scraping' ? '候选数据仍在抓取中' : '最终结果正在处理中');
                    pollInteractiveRescrape(sessionId, true);
                }
            } catch (e) {
                showToast(e.message || '待选择数据加载失败', 'error');
            }
        }

        function rescrapeDisplayValue(value) {
            if (value === null || value === undefined || value === '' || (Array.isArray(value) && !value.length)) return '空';
            if (Array.isArray(value)) return value.join('、');
            return String(value);
        }

        function openRescrapeEditorData(data) {
            _rescrapeState.data = data;
            _rescrapeState.sessionId = data.session.id;
            _rescrapeState.taskId = data.task?.id || null;
            _rescrapeState.autoOpened.add(data.session.id);
            document.getElementById('rescrapeEditorTitle').textContent = `${data.task?.avid || '影片'} · 字段选择`;
            document.getElementById('rescrapeEditorSubtitle').textContent = data.media_note || '媒体文件将在确认后才处理';
            const body = document.getElementById('rescrapeEditorBody');
            body.innerHTML = (data.fields || []).map(field => {
                const sourceButtons = (field.sources || []).map((source, index) => `
                    <button class="rescrape-candidate ${source.present ? '' : 'empty'}" data-rescrape-field="${field.field}" data-rescrape-origin="source" data-rescrape-index="${index}">
                        <span class="rescrape-candidate-source">${escapeHtml(source.crawler_name)}</span>
                        <span class="rescrape-candidate-value">${escapeHtml(rescrapeDisplayValue(source.value))}</span>
                    </button>
                `).join('');
                const input = field.editor === 'textarea'
                    ? `<textarea class="rescrape-final-input" id="rescrape-field-${field.field}" data-rescrape-input="${field.field}"></textarea>`
                    : `<input class="rescrape-final-input" id="rescrape-field-${field.field}" data-rescrape-input="${field.field}" type="${field.editor === 'number' ? 'number' : 'text'}" ${field.editor === 'number' ? 'step="any"' : ''}>`;
                return `
                    <section class="rescrape-field-card" data-field="${field.field}">
                        <div class="rescrape-field-heading"><strong>${escapeHtml(field.label)}</strong><code>${escapeHtml(field.field)}</code></div>
                        <div class="rescrape-candidate-list">
                            <button class="rescrape-candidate auto" data-rescrape-field="${field.field}" data-rescrape-origin="auto">
                                <span class="rescrape-candidate-source">自动推荐</span>
                                <span class="rescrape-candidate-value">${escapeHtml(rescrapeDisplayValue(field.auto_value))}</span>
                            </button>
                            <button class="rescrape-candidate current" data-rescrape-field="${field.field}" data-rescrape-origin="current">
                                <span class="rescrape-candidate-source">保留当前</span>
                                <span class="rescrape-candidate-value">${escapeHtml(rescrapeDisplayValue(field.current_value))}</span>
                            </button>
                            ${sourceButtons}
                        </div>
                        <div class="rescrape-final-row">
                            <span class="rescrape-final-label" id="rescrape-source-${field.field}">最终值 · 自动推荐</span>
                            ${input}
                            <button class="rescrape-clear-btn" data-rescrape-clear="${field.field}">清空</button>
                        </div>
                    </section>
                `;
            }).join('');
            (data.fields || []).forEach(field => writeRescrapeFieldValue(field.field, field.auto_value, '自动推荐'));
            const overlay = document.getElementById('rescrapeEditorOverlay');
            overlay.classList.add('open');
            overlay.setAttribute('aria-hidden', 'false');
        }

        function closeRescrapeEditor() {
            const overlay = document.getElementById('rescrapeEditorOverlay');
            overlay.classList.remove('open');
            overlay.setAttribute('aria-hidden', 'true');
        }

        function rescrapeFieldDefinition(fieldName) {
            return (_rescrapeState.data?.fields || []).find(field => field.field === fieldName);
        }

        function writeRescrapeFieldValue(fieldName, value, sourceLabel) {
            const input = document.getElementById(`rescrape-field-${fieldName}`);
            if (!input) return;
            input.value = Array.isArray(value) ? value.join(', ') : (value ?? '');
            input.dataset.source = sourceLabel || '手动编辑';
            const label = document.getElementById(`rescrape-source-${fieldName}`);
            if (label) label.textContent = `最终值 · ${input.dataset.source}`;
        }

        function selectRescrapeValue(fieldName, origin, sourceIndex) {
            const field = rescrapeFieldDefinition(fieldName);
            if (!field) return;
            let value;
            let label;
            if (origin === 'current') {
                value = field.current_value;
                label = '保留当前';
            } else if (origin === 'source') {
                const source = field.sources[sourceIndex];
                value = source?.value;
                label = source?.crawler_name || '爬虫来源';
            } else {
                value = field.auto_value;
                label = '自动推荐';
            }
            writeRescrapeFieldValue(fieldName, value, label);
        }

        function markRescrapeManual(fieldName) {
            const input = document.getElementById(`rescrape-field-${fieldName}`);
            if (!input) return;
            input.dataset.source = '手动编辑';
            const label = document.getElementById(`rescrape-source-${fieldName}`);
            if (label) label.textContent = '最终值 · 手动编辑';
        }

        function clearRescrapeField(fieldName) {
            const field = rescrapeFieldDefinition(fieldName);
            if (!field || !confirm(`确定清空“${field.label}”吗？该操作会覆盖现有值。`)) return;
            writeRescrapeFieldValue(fieldName, field.editor === 'tags' ? [] : null, '主动清空');
        }

        function applyAllAutoRescrapeValues() {
            (_rescrapeState.data?.fields || []).forEach(field => writeRescrapeFieldValue(field.field, field.auto_value, '自动推荐'));
        }

        function applyAllCurrentRescrapeValues() {
            (_rescrapeState.data?.fields || []).forEach(field => writeRescrapeFieldValue(field.field, field.current_value, '保留当前'));
        }

        function readRescrapeFinalValues() {
            const values = {};
            (_rescrapeState.data?.fields || []).forEach(field => {
                const input = document.getElementById(`rescrape-field-${field.field}`);
                const text = input ? input.value.trim() : '';
                values[field.field] = field.editor === 'tags'
                    ? text.split(/[,，、\n]+/).map(item => item.trim()).filter(Boolean)
                    : (text || null);
            });
            return values;
        }

        async function submitInteractiveRescrape() {
            if (!_rescrapeState.sessionId) return;
            if (!confirm('确认使用当前最终值更新数据库和 NFO，并在此后开始下载封面、剧照等媒体文件吗？')) return;
            const button = document.getElementById('rescrapeApplyBtn');
            button.disabled = true;
            try {
                const res = await fetch(`/api/rescrape-sessions/${_rescrapeState.sessionId}/apply`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({values: readRescrapeFinalValues()})
                });
                const data = await res.json();
                if (!res.ok) {
                    showToast(data.message || '提交失败', 'error');
                    return;
                }
                showToast(data.message || '开始应用最终结果');
                closeRescrapeEditor();
                pollInteractiveRescrape(_rescrapeState.sessionId, false);
                loadTaskCenter();
                ensureTaskPolling();
            } catch (e) {
                showToast('网络错误，请稍后重试', 'error');
            } finally {
                button.disabled = false;
            }
        }

        async function cancelInteractiveRescrape() {
            if (!_rescrapeState.sessionId || !confirm('确定取消本次重新刮削吗？现有元数据、NFO 和媒体文件都不会改变。')) return;
            try {
                const res = await fetch(`/api/rescrape-sessions/${_rescrapeState.sessionId}/cancel`, {method: 'POST'});
                const data = await res.json();
                showToast(data.message || (res.ok ? '已取消' : '取消失败'), res.ok ? 'success' : 'error');
                if (res.ok) closeRescrapeEditor();
                loadTaskCenter();
            } catch (e) {
                showToast('网络错误，请稍后重试', 'error');
            }
        }

        function maybeAutoOpenAwaitingRescrape() {
            const waiting = (_taskCenterState.allTasks || []).find(task =>
                task.rescrape_session?.status === 'awaiting_selection'
                && !_rescrapeState.autoOpened.has(task.rescrape_session.id)
            );
            if (waiting && !document.getElementById('rescrapeEditorOverlay')?.classList.contains('open')) {
                continueInteractiveRescrape(waiting.rescrape_session.id);
            }
        }

        function showAwaitingRescrapes() {
            const waiting = (_taskCenterState.allTasks || []).find(task => task.rescrape_session?.status === 'awaiting_selection');
            if (waiting) continueInteractiveRescrape(waiting.rescrape_session.id);
            else showToast('当前没有待人工选择的任务');
        }

        // 这些函数由动态生成的按钮通过内联事件调用，显式挂到 window 可避免浏览器隔离执行环境差异。
        Object.assign(window, {
            rescrapeTaskMetadata,
            closeRescrapeMode,
            startRescrapeMode,
            continueInteractiveRescrape,
            closeRescrapeEditor,
            selectRescrapeValue,
            markRescrapeManual,
            clearRescrapeField,
            applyAllAutoRescrapeValues,
            applyAllCurrentRescrapeValues,
            submitInteractiveRescrape,
            cancelInteractiveRescrape,
            showAwaitingRescrapes
        });
        async function checkMetadataComplete() {
            try {
                const res = await fetch('/api/metadata/check_complete', {method: 'POST'});
                const data = await res.json();
                showToast(data.message, res.ok ? 'success' : 'error');
                loadTaskCenter();
                refreshMovieListFromTasks({scope: _movieListScope});
                loadTaskDashboard();
            } catch(e) { showToast('网络错误', 'error'); }
        }

        async function refreshIncompleteMetadata() {
            try {
                const res = await fetch('/api/metadata/refresh_incomplete', {method: 'POST'});
                const data = await res.json();
                showToast(data.message || (res.ok ? '优化任务已启动' : '优化启动失败'), res.ok ? 'success' : 'error');
                loadTaskCenter();
                ensureTaskPolling();
            } catch (e) {
                showToast('网络错误，请稍后重试', 'error');
            }
        }

        async function importLegacyMetadata() {
            const input = document.getElementById('legacyImportPath');
            const path = input ? input.value : '';
            if (!path || !path.trim()) return;
            localStorage.setItem('legacyImportPath', path.trim());
            try {
                const res = await fetch('/api/metadata/import_legacy', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({path: path.trim()})
                });
                const data = await res.json();
                const summary = data.summary || {};
                const summaryEl = document.getElementById('legacyImportSummary');
                if (summaryEl) {
                    summaryEl.textContent = res.ok
                        ? `扫描 ${summary.scanned || 0}，新增 ${summary.created || 0}，更新 ${summary.updated || 0}，待优化 ${summary.incomplete || 0}`
                        : '导入失败';
                }
                showToast(data.message || (res.ok ? '历史目录导入完成' : '历史目录导入失败'), res.ok ? 'success' : 'error');
                loadTaskCenter();
                refreshMovieListFromTasks({scope: _movieListScope});
                loadTaskDashboard();
            } catch (e) {
                showToast('网络错误，请稍后重试', 'error');
            }
        }

        async function normalizeActressMetadata() {
            const input = document.getElementById('legacyImportPath');
            const path = input ? input.value : '';
            if (!path || !path.trim()) return;
            localStorage.setItem('legacyImportPath', path.trim());
            const move = !!document.getElementById('normalizeActressMove')?.checked;
            _taskCenterState.normalizeActressStartedAt = Date.now() / 1000 - 2;
            _taskCenterState.normalizeActressCompletedRunId = null;
            setNormalizeActressRunning(true);
            const summaryEl = document.getElementById('normalizeActressSummary');
            if (summaryEl) summaryEl.textContent = '归一化运行中...';
            try {
                const res = await fetch('/api/metadata/normalize_actress', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({path: path.trim(), move})
                });
                const data = await res.json();
                if (summaryEl && !res.ok) summaryEl.textContent = '归一化启动失败';
                if (!res.ok) setNormalizeActressRunning(false);
                showToast(data.message || (res.ok ? '女优名归一化已启动' : '女优名归一化启动失败'), res.ok ? 'success' : 'error');
                loadTaskCenter();
                ensureTaskPolling();
            } catch (e) {
                setNormalizeActressRunning(false);
                if (summaryEl) summaryEl.textContent = '归一化启动失败';
                showToast('网络错误，请稍后重试', 'error');
            }
        }


        function filterMetadataByRun(filterValue, filterType) {
            switchTaskSection('metadata');
            _taskCenterState.metadataRunFilter = filterValue;
            _taskCenterState.metadataRunFilterType = filterType || 'run_id';
            renderTaskCenter();
        }

        function renderTaskCenter() {
            const allTasks = _taskCenterState.allTasks || [];
            const activities = _taskCenterState.activities || [];
            const crawlers = _taskCenterState.crawlers || [];
            const counts = {};
            allTasks.forEach(t => { counts[t.status] = (counts[t.status] || 0) + 1; });

            const run = _taskCenterState.latestRun;
            document.getElementById('taskCenterLatestRun').textContent = run
                ? `${run.status}｜${formatTime(run.finished_at || run.started_at)}`
                : '尚无记录';
            document.getElementById('taskCenterPending').textContent = counts.pending || 0;
            document.getElementById('taskCenterFailed').textContent = counts.failed || 0;
            document.getElementById('taskCenterDeferred').textContent = counts.deferred || 0;
            const awaitingFromTasks = allTasks.filter(task => task.rescrape_session?.status === 'awaiting_selection').length;
            document.getElementById('taskCenterAwaitingSelection').textContent = _taskCenterState.polling.awaiting_selection_count ?? awaitingFromTasks;

            const savedLegacyPath = localStorage.getItem('legacyImportPath') || '';
            const legacyInput = document.getElementById('legacyImportPath');
            if (legacyInput && !legacyInput.value) legacyInput.value = savedLegacyPath;

            const metadataFilter = document.getElementById('taskFilterMetadata')?.value || '';
            const searchText = (document.getElementById('taskFilterSearch')?.value || '').trim().toLowerCase();
            const queueTasks = allTasks.filter(task => ['pending', 'running', 'deferred', 'failed'].includes(task.status));
            const successTasks = allTasks.filter(task => task.status === 'success');
            let metadataTasks = successTasks.filter(task => {
                if (metadataFilter && task.metadata_status !== metadataFilter) return false;
                if (_taskCenterState.metadataRunFilter) {
                    if (_taskCenterState.metadataRunFilterType === 'task_id') {
                        if (Number(task.id) !== Number(_taskCenterState.metadataRunFilter)) return false;
                    } else {
                        if (Number(task.last_run_id) !== Number(_taskCenterState.metadataRunFilter)) return false;
                    }
                }
                if (searchText) {
                    const haystack = `${task.avid || ''} ${taskPath(task)} ${task.current_save_dir || ''}`.toLowerCase();
                    if (!haystack.includes(searchText)) return false;
                }
                return true;
            });

            const p = _taskCenterState.polling || {};
            document.getElementById('taskCenterRefreshRunning').textContent = p.refresh_running || 0;
            if (p.refresh_starting) {
                document.getElementById('taskCenterRefreshRunning').textContent = '启动中';
                document.getElementById('taskCenterRefreshRunning').style.color = '#f59e0b';
            } else if (p.refresh_running > 0) {
                document.getElementById('taskCenterRefreshRunning').style.color = '#a78bfa';
            } else {
                document.getElementById('taskCenterRefreshRunning').style.color = '';
            }
            const incompleteFromTasks = successTasks.filter(t => t.metadata_status === 'incomplete').length;
            document.getElementById('taskCenterIncomplete').textContent = p.incomplete_count || incompleteFromTasks;

            const runFilterLabel = _taskCenterState.metadataRunFilter
                ? ` (${_taskCenterState.metadataRunFilterType === 'task_id' ? 'Task' : 'Run'} #${_taskCenterState.metadataRunFilter})`
                : '';
            const queuePage = pageItems('queue', queueTasks);
            document.getElementById('queueTableCount').textContent = `${queueTasks.length} 条`;
            document.getElementById('queueTableBody').innerHTML = queuePage.items.length ? queuePage.items.map(task => `
                <tr>
                    <td><input type="checkbox" ${isTaskSelected('queue', task.id) ? 'checked' : ''} onchange="toggleTaskSelection('queue', ${task.id})"></td>
                    <td>${task.id}</td>
                    <td>${escapeHtml(task.avid)}</td>
                    <td>${statusBadge(task.status)}</td>
                    <td>${escapeHtml(task.data_src)}</td>
                    <td>${escapeHtml(task.failure_stage || '-')}</td>
                    <td>${escapeHtml(task.failure_reason || '-')}</td>
                    <td class="path-cell">${escapeHtml(taskPath(task))}</td>
                    <td><div class="task-actions">
                        <button class="task-action-btn" onclick="openTaskDetail(${task.id})">详情</button>
                        <button class="task-action-btn retry" onclick="retryTask(${task.id})" ${['failed', 'deferred'].includes(task.status) ? '' : 'disabled'}>重试</button>
                        <button class="task-action-btn" onclick="deleteTaskRecord(${task.id})">删除</button>
                    </div></td>
                </tr>
            `).join('') : '<tr><td colspan="9" class="task-muted">暂无待处理队列</td></tr>';
            renderPagination('queue', queueTasks.length);
            document.getElementById('queueBatchRetryBtn').disabled = (_taskCenterState.selections.queue?.size || 0) === 0;
            document.getElementById('queueBatchDelBtn').disabled = (_taskCenterState.selections.queue?.size || 0) === 0;
            document.getElementById('queueSelectAll').checked = queuePage.items.length > 0 && queuePage.items.every(t => isTaskSelected('queue', t.id));

            const metadataPage = pageItems('metadata', metadataTasks);
            document.getElementById('metadataTableCount').textContent = `${metadataTasks.length} / ${successTasks.length} 条${runFilterLabel}`;
            document.getElementById('metadataTableBody').innerHTML = metadataPage.items.length ? metadataPage.items.map(task => `
                <tr>
                    <td><input type="checkbox" ${isTaskSelected('metadata', task.id) ? 'checked' : ''} onchange="toggleTaskSelection('metadata', ${task.id})"></td>
                    <td>${task.id}</td>
                    <td>${escapeHtml(task.avid)}</td>
                    <td>${metadataStatusBadge(task.metadata_status)} ${rescrapeSessionBadge(task.rescrape_session)}</td>
                    <td>${escapeHtml(taskTypeLabel(task))}</td>
                    <td class="path-cell">${escapeHtml(task.current_nfo_path || '-')}</td>
                    <td class="path-cell">${escapeHtml(task.current_save_dir || task.save_dir || '-')}</td>
                    <td><div class="task-actions">
                        <button class="task-action-btn" onclick="openTaskDetail(${task.id})">详情</button>
                        <button class="task-action-btn" onclick="checkTaskMetadata(${task.id})">检查</button>
                        <button class="task-action-btn" onclick="refreshTaskMetadata(${task.id})">优化</button>
                        ${taskRescrapeActionHtml(task)}
                        <button class="task-action-btn" onclick="deleteTaskRecord(${task.id})">删除</button>
                    </div></td>
                </tr>
            `).join('') : '<tr><td colspan="8" class="task-muted">暂无影片元数据记录</td></tr>';
            renderPagination('metadata', metadataTasks.length);
            document.getElementById('metaBatchDelBtn').disabled = (_taskCenterState.selections.metadata?.size || 0) === 0;
            document.getElementById('metaSelectAll').checked = metadataPage.items.length > 0 && metadataPage.items.every(t => isTaskSelected('metadata', t.id));

            const activityPage = pageItems('activity', activities);
            document.getElementById('activityTableCount').textContent = `${activities.length} 条`;
            document.getElementById('activityTableBody').innerHTML = activityPage.items.length ? activityPage.items.map(run => `
                <tr>
                    <td>${escapeHtml(activityLabel(run))}</td>
                    <td>${statusBadge(run.status)}</td>
                    <td>${escapeHtml(formatTime(run.finished_at || run.started_at))}</td>
                    <td>${escapeHtml(activitySummary(run))}</td>
                    <td><div class="task-actions">${(run.activity_type === 'metadata_refresh' && run.task_id) ? `<button class="task-action-btn" onclick="openTaskDetail(${run.task_id})">详情</button><button class="task-action-btn" onclick="filterMetadataByRun(${run.task_id}, 'task_id')">涉及任务</button>`
                       : (run.activity_type === 'scrape_run' && run.id) ? `<button class="task-action-btn" onclick="openRunDetail(${run.id})">详情</button><button class="task-action-btn" onclick="filterMetadataByRun(${run.id}, 'run_id')">涉及任务</button>`
                       : ''}</div></td>
                </tr>
            `).join('') : '<tr><td colspan="5" class="task-muted">暂无运行记录</td></tr>';
            renderPagination('activity', activities.length);

            const healthPage = pageItems('health', crawlers);
            document.getElementById('crawlerHealthCount').textContent = `${crawlers.length} 个`;
            document.getElementById('crawlerHealthBody').innerHTML = healthPage.items.length ? healthPage.items.map(crawler => `
                <tr>
                    <td>${escapeHtml(crawler.name)}</td>
                    <td>${crawler.success_count || 0}</td>
                    <td>${crawler.failure_count || 0}</td>
                    <td>${crawler.consecutive_failures || 0}</td>
                    <td>${crawler.circuit_open ? '<span class="status-badge failed">熔断</span>' : '<span class="status-badge success">正常</span>'}</td>
                </tr>
            `).join('') : '<tr><td colspan="5" class="task-muted">暂无爬虫统计</td></tr>';
            renderPagination('health', crawlers.length);
        }

        async function loadTaskCenter() {
            try {
                const sortCol = _taskCenterState.sortCol;
                const sortDir = _taskCenterState.sortDir;
                const [runRes, activityRes, taskRes, healthRes, pollRes] = await Promise.all([
                    fetch('/api/runs/latest').then(r => r.json()),
                    fetch('/api/runs/activity?limit=200').then(r => r.json()),
                    fetch(`/api/tasks?limit=500&order_by=${sortCol}&sort_dir=${sortDir}`).then(r => r.json()),
                    fetch('/api/crawler_health').then(r => r.json()),
                    fetch('/api/polling_active').then(r => r.json())
                ]);
                _taskCenterState.latestRun = runRes.run;
                _taskCenterState.activities = activityRes.runs || [];
                _taskCenterState.allTasks = taskRes.tasks || [];
                _taskCenterState.totalTasks = taskRes.total || 0;
                _taskCenterState.crawlers = healthRes.crawlers || [];
                if (pollRes) { _taskCenterState.polling = pollRes; }
                renderTaskCenter();
                updateNormalizeActressStatus(_taskCenterState.activities, _taskCenterState.polling);
                maybeAutoOpenAwaitingRescrape();
            } catch (e) {
                console.error('Failed to load task center:', e);
                showToast('任务中心加载失败', 'error');
            }
        }

        // --- 配置管理（新版表单 + YAML 双模式）---
        let _configData = {};
        let _schedulerData = {};
        let _currentConfigSection = 'scanner';
        const METADATA_FIELDS = [
            // field, label, has_language_pref, has_min_items, has_reject
            {field:'title', label:'标题', lang:true, items:false, reject:false},
            {field:'plot', label:'剧情', lang:true, items:false, reject:true},
            {field:'score', label:'评分', lang:false, items:false, reject:false},
            {field:'genre', label:'类别', lang:true, items:true, reject:false},
            {field:'preview_pics', label:'剧照', lang:false, items:true, reject:false},
            {field:'director', label:'导演', lang:false, items:false, reject:false},
            {field:'duration', label:'时长', lang:false, items:false, reject:false},
            {field:'producer', label:'制作商', lang:false, items:false, reject:false},
            {field:'publisher', label:'发行商', lang:false, items:false, reject:false},
            {field:'publish_date', label:'发布日期', lang:false, items:false, reject:false},
            {field:'actress_pics', label:'女优头像', lang:false, items:true, reject:false},
        ];

        function showConfigSection(section) {
            _currentConfigSection = section;
            document.querySelectorAll('.nav-item[data-section]').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.section === section);
            });
            document.querySelectorAll('.form-section').forEach(el => {
                el.classList.toggle('active', el.id === 'section-' + section);
            });
        }

        function toggleYamlMode() {
            const formView = document.getElementById('formView');
            const yamlView = document.getElementById('yamlView');
            const isYaml = yamlView.style.display !== 'none';
            if (isYaml) {
                yamlView.style.display = 'none';
                formView.style.display = 'block';
                document.querySelector('.yaml-nav').textContent = '🔧 YAML 高级模式';
            } else {
                formView.style.display = 'none';
                yamlView.style.display = 'block';
                document.querySelector('.yaml-nav').textContent = '← 返回表单模式';
            }
        }

        function getPath(obj, path, defaultValue) {
            const keys = path.split('.');
            let current = obj;
            for (const key of keys) {
                if (current === null || current === undefined || !(key in current)) {
                    return defaultValue;
                }
                current = current[key];
            }
            return current === null || current === undefined ? defaultValue : current;
        }

        function setPath(obj, path, value) {
            const keys = path.split('.');
            let current = obj;
            for (let i = 0; i < keys.length - 1; i++) {
                const key = keys[i];
                if (!(key in current) || current[key] === null || typeof current[key] !== 'object') {
                    current[key] = {};
                }
                current = current[key];
            }
            const lastKey = keys[keys.length - 1];
            if (value === '' && (path === 'network.proxy_server' || path === 'translator.engine' || path === 'scanner.input_directory')) {
                current[lastKey] = null;
            } else {
                current[lastKey] = value;
            }
        }

        function defaultMetadataRule(field) {
            const defaults = {
                title: {required: true, prefer_language: 'zh', min_cjk_ratio: 0.3, allow_overwrite: true},
                plot: {required: true, prefer_language: 'zh', min_cjk_ratio: 0.5, min_length: 20, reject_values: ['暂无简介', '暂无剧情', '无', '-'], allow_overwrite: true},
                genre: {required: false, min_items: 2},
                preview_pics: {required: false, min_items: 3},
                actress_pics: {required: false, min_items: 1}
            };
            return Object.assign({required: false, prefer_language: null, min_cjk_ratio: 0, min_length: 0, min_items: 0, reject_values: [], allow_overwrite: false}, defaults[field] || {});
        }

        function renderMetadataFields() {
            const fields = getPath(_configData, 'metadata_complete.fields', {});
            const tbody = document.getElementById('metadataFieldsBody');
            if (!tbody) return;
            const hasLang = METADATA_FIELDS.some(f => f.lang);
            tbody.innerHTML = METADATA_FIELDS.map(({field, label, lang, items, reject}) => {
                const rule = Object.assign(defaultMetadataRule(field), fields[field] || {});
                const rejectValues = reject && Array.isArray(rule.reject_values) ? rule.reject_values.join(',') : '';
                let row = `<tr data-field="${field}" data-lang="${lang}" data-items="${items}" data-reject="${reject}">
                    <td>${label}<div class="form-hint">${field}</div></td>
                    <td><input type="checkbox" data-key="required" ${rule.required ? 'checked' : ''}></td>`;
                if (lang) {
                    row += `<td><input type="checkbox" data-key="prefer_zh" ${rule.prefer_language === 'zh' ? 'checked' : ''}></td>`;
                    row += `<td><input class="metadata-mini-input" type="number" step="0.1" min="0" max="1" data-key="min_cjk_ratio" value="${rule.min_cjk_ratio || 0}"></td>`;
                    row += `<td><input class="metadata-mini-input" type="number" min="0" data-key="min_length" value="${rule.min_length || 0}"></td>`;
                } else {
                    row += `<td></td><td></td><td></td>`;
                }
                if (items) {
                    row += `<td><input class="metadata-mini-input" type="number" min="0" data-key="min_items" value="${rule.min_items || 0}"></td>`;
                } else {
                    row += `<td></td>`;
                }
                if (reject) {
                    row += `<td><input class="metadata-mini-input" type="text" data-key="reject_values" value="${escapeHtml(rejectValues)}"></td>`;
                } else {
                    row += `<td></td>`;
                }
                row += `</tr>`;
                return row;
            }).join('');
        }

        function fillForm() {
            const cfg = _configData;

            // scanner
            const inputDir = getPath(cfg, 'scanner.input_directory', '');
            document.getElementById('scanner_input_directory').value = inputDir === null ? '' : inputDir;
            document.getElementById('scanner_minimum_size').value = getPath(cfg, 'scanner.minimum_size', '100MiB');
            document.getElementById('scanner_skip_nfo_dir').checked = getPath(cfg, 'scanner.skip_nfo_dir', true);
            const ignoredFolders = getPath(cfg, 'scanner.ignored_folder_name_pattern', ['^\\.', '^#recycle$', '^#整理完成$', '^#不要扫描$','^JAV$']);
            document.getElementById('scanner_ignored_folder_name_pattern').value = Array.isArray(ignoredFolders) ? ignoredFolders.join('\n') : '';
            const fileExts = getPath(cfg, 'scanner.filename_extensions', ['.mp4', '.mkv', '.avi', '.wmv', '.mov', '.flv', '.m4v', '.webm', '.mpeg', '.mpg', '.ts', '.m2ts', '.iso', '.vob', '.rm', '.rmvb', '.3gp', '.f4v', '.strm']);
            document.querySelectorAll('#scanner_filename_extensions input').forEach(cb => {
                cb.checked = fileExts.includes(cb.value);
                cb.closest('.checkbox-item').classList.toggle('checked', cb.checked);
            });

            // network
            const proxy = getPath(cfg, 'network.proxy_server', '');
            document.getElementById('network_proxy_server').value = proxy === null ? '' : proxy;
            document.getElementById('network_retry').value = getPath(cfg, 'network.retry', 3);
            document.getElementById('network_timeout').value = getPath(cfg, 'network.timeout', 'PT10S');

            // crawler
            const requiredKeys = getPath(cfg, 'crawler.required_keys', ['cover', 'title']);
            document.querySelectorAll('#crawler_required_keys input').forEach(cb => {
                cb.checked = requiredKeys.includes(cb.value);
                cb.closest('.checkbox-item').classList.toggle('checked', cb.checked);
            });
            document.getElementById('crawler_hardworking').checked = getPath(cfg, 'crawler.hardworking', true);
            document.getElementById('crawler_respect_site_avid').checked = getPath(cfg, 'crawler.respect_site_avid', true);
            document.getElementById('crawler_sleep_after_scraping').value = getPath(cfg, 'crawler.sleep_after_scraping', 'PT1S');
            document.getElementById('crawler_use_javdb_cover').value = getPath(cfg, 'crawler.use_javdb_cover', 'fallback');
            document.getElementById('crawler_normalize_actress_name').checked = getPath(cfg, 'crawler.normalize_actress_name', true);
            const javdbCookie = getPath(cfg, 'network.javdb_cookie', '');
            document.getElementById('network_javdb_cookie').value = javdbCookie === null ? '' : javdbCookie;

            // metadata complete
            document.getElementById('metadata_enabled').checked = getPath(cfg, 'metadata_complete.enabled', true);
            document.getElementById('metadata_auto_refresh').checked = getPath(cfg, 'metadata_complete.auto_refresh', true);
            document.getElementById('metadata_refresh_after').value = getPath(cfg, 'metadata_complete.refresh_after', 'P7D');
            renderMetadataFields();

            // summarizer
            document.getElementById('summarizer_move_files').checked = getPath(cfg, 'summarizer.move_files', false);
            document.getElementById('summarizer_path_output_folder_pattern').value = getPath(cfg, 'summarizer.path.output_folder_pattern', '');
            document.getElementById('summarizer_path_basename_pattern').value = getPath(cfg, 'summarizer.path.basename_pattern', '');
            document.getElementById('summarizer_path_hard_link').checked = getPath(cfg, 'summarizer.path.hard_link', false);
            document.getElementById('summarizer_title_remove_trailing_actor_name').checked = getPath(cfg, 'summarizer.title.remove_trailing_actor_name', true);
            document.getElementById('summarizer_default_title').value = getPath(cfg, 'summarizer.default.title', '');
            document.getElementById('summarizer_default_actress').value = getPath(cfg, 'summarizer.default.actress', '');
            document.getElementById('summarizer_nfo_title_pattern').value = getPath(cfg, 'summarizer.nfo.title_pattern', '');
            document.getElementById('summarizer_nfo_include_actor_tmdbid').checked = getPath(cfg, 'summarizer.nfo.include_actor_tmdbid', true);
            const censor = getPath(cfg, 'summarizer.censor_options_representation', ['无码', '有码', '打码情况未知']);
            document.getElementById('summarizer_censor_0').value = censor[0] || '';
            document.getElementById('summarizer_censor_1').value = censor[1] || '';
            document.getElementById('summarizer_censor_2').value = censor[2] || '';
            document.getElementById('summarizer_cover_highres').checked = getPath(cfg, 'summarizer.cover.highres', true);
            document.getElementById('summarizer_cover_add_label').checked = getPath(cfg, 'summarizer.cover.add_label', false);
            document.getElementById('summarizer_fanart_basename_pattern').value = getPath(cfg, 'summarizer.fanart.basename_pattern', '');
            document.getElementById('summarizer_extra_fanarts_enabled').checked = getPath(cfg, 'summarizer.extra_fanarts.enabled', false);
            document.getElementById('summarizer_extra_fanarts_scrap_interval').value = getPath(cfg, 'summarizer.extra_fanarts.scrap_interval', 'PT1.5S');

            // translator
            const engine = getPath(cfg, 'translator.engine', null);
            const engineName = engine === null ? '' : (typeof engine === 'object' ? (engine.name || '') : engine);
            document.getElementById('translator_engine').value = engineName;
            // 清空并填充引擎特定字段
            const openaiUrl = document.getElementById('translator_openai_url');
            const openaiKey = document.getElementById('translator_openai_api_key');
            const openaiModel = document.getElementById('translator_openai_model');
            const openaiPrompt = document.getElementById('translator_openai_system_prompt');
            if (openaiUrl) openaiUrl.value = '';
            if (openaiKey) openaiKey.value = '';
            if (openaiModel) openaiModel.value = '';
            if (openaiPrompt) openaiPrompt.value = '';
            if (engine && typeof engine === 'object') {
                if (engineName === 'openai') {
                    if (openaiUrl) openaiUrl.value = engine.url || '';
                    if (openaiKey) openaiKey.value = engine.api_key || '';
                    if (openaiModel) openaiModel.value = engine.model || '';
                    if (openaiPrompt) openaiPrompt.value = engine.system_prompt || '';
                }
            }
            document.getElementById('translator_fields_title').checked = getPath(cfg, 'translator.fields.title', true);
            document.getElementById('translator_fields_plot').checked = getPath(cfg, 'translator.fields.plot', true);
            onTranslatorEngineChange();

            // scheduler
            const sched = _schedulerData.scheduler || {};
            document.getElementById('scheduler_enabled').checked = sched.enabled !== false;
            document.getElementById('scheduler_mode').value = sched.mode || 'cron';
            const cron = sched.cron || {};
            document.getElementById('scheduler_cron_time').value = cron.time || '03:00';
            const interval = sched.interval || {};
            document.getElementById('scheduler_interval_hours').value = interval.hours || 0;
            document.getElementById('scheduler_interval_minutes').value = interval.minutes || 0;
            onSchedulerModeChange();

            // refresh scheduler
            const rsched = _schedulerData.refresh_scheduler || {};
            document.getElementById('refresh_scheduler_enabled').checked = rsched.enabled !== false;
            document.getElementById('refresh_scheduler_mode').value = rsched.mode || 'interval';
            const rcron = rsched.cron || {};
            document.getElementById('refresh_scheduler_cron_time').value = rcron.time || '04:00';
            const rinterval = rsched.interval || {};
            document.getElementById('refresh_scheduler_interval_hours').value = rinterval.hours || 24;
            document.getElementById('refresh_scheduler_interval_minutes').value = rinterval.minutes || 0;
            onRefreshSchedulerModeChange();
        }

        function onTranslatorEngineChange() {
            const engine = document.getElementById('translator_engine').value;
            const container = document.getElementById('translator_engine_fields');
            if (!engine) {
                container.style.display = 'none';
                return;
            }
            container.style.display = 'block';
            ['openai'].forEach(name => {
                const el = document.getElementById('field_' + name);
                if (el) el.style.display = name === engine ? 'block' : 'none';
            });
        }

        function onSchedulerModeChange() {
            const enabled = document.getElementById('scheduler_enabled').checked;
            const mode = document.getElementById('scheduler_mode').value;
            const settingsEl = document.getElementById('scheduler_settings');
            if (settingsEl) settingsEl.style.display = enabled ? 'block' : 'none';
            const cronEl = document.getElementById('scheduler_cron_settings');
            if (cronEl) cronEl.style.display = mode === 'cron' ? 'block' : 'none';
            const intervalEl = document.getElementById('scheduler_interval_settings');
            if (intervalEl) intervalEl.style.display = mode === 'interval' ? 'block' : 'none';
        }

        function onRefreshSchedulerModeChange() {
            const enabled = document.getElementById('refresh_scheduler_enabled').checked;
            const mode = document.getElementById('refresh_scheduler_mode').value;
            const settingsEl = document.getElementById('refresh_scheduler_settings');
            if (settingsEl) settingsEl.style.display = enabled ? 'block' : 'none';
            const cronEl = document.getElementById('refresh_scheduler_cron_settings');
            if (cronEl) cronEl.style.display = mode === 'cron' ? 'block' : 'none';
            const intervalEl = document.getElementById('refresh_scheduler_interval_settings');
            if (intervalEl) intervalEl.style.display = mode === 'interval' ? 'block' : 'none';
        }

        function collectFormData() {
            const cfg = _configData;

            // scanner
            setPath(cfg, 'scanner.input_directory', document.getElementById('scanner_input_directory').value || null);
            setPath(cfg, 'scanner.minimum_size', document.getElementById('scanner_minimum_size').value || '100MiB');
            setPath(cfg, 'scanner.skip_nfo_dir', document.getElementById('scanner_skip_nfo_dir').checked);
            const ignoredPatternText = document.getElementById('scanner_ignored_folder_name_pattern').value.trim();
            const ignoredPatterns = ignoredPatternText ? ignoredPatternText.split('\n').map(s => s.trim()).filter(s => s) : ['^\\.', '^#recycle$', '^#整理完成$', '^#不要扫描$','^JAV$'];
            setPath(cfg, 'scanner.ignored_folder_name_pattern', ignoredPatterns);
            const selectedExts = [];
            document.querySelectorAll('#scanner_filename_extensions input:checked').forEach(cb => selectedExts.push(cb.value));
            setPath(cfg, 'scanner.filename_extensions', selectedExts.length ? selectedExts : ['.mp4', '.mkv', '.avi']);
            // network
            const proxyVal = document.getElementById('network_proxy_server').value.trim();
            setPath(cfg, 'network.proxy_server', proxyVal || null);
            setPath(cfg, 'network.retry', parseInt(document.getElementById('network_retry').value) || 3);
            setPath(cfg, 'network.timeout', document.getElementById('network_timeout').value || 'PT10S');

            // crawler
            const requiredKeys = [];
            document.querySelectorAll('#crawler_required_keys input:checked').forEach(cb => requiredKeys.push(cb.value));
            setPath(cfg, 'crawler.required_keys', requiredKeys.length ? requiredKeys : ['cover', 'title']);
            setPath(cfg, 'crawler.hardworking', document.getElementById('crawler_hardworking').checked);
            setPath(cfg, 'crawler.respect_site_avid', document.getElementById('crawler_respect_site_avid').checked);
            setPath(cfg, 'crawler.sleep_after_scraping', document.getElementById('crawler_sleep_after_scraping').value || 'PT1S');
            setPath(cfg, 'crawler.use_javdb_cover', document.getElementById('crawler_use_javdb_cover').value);
            setPath(cfg, 'crawler.normalize_actress_name', document.getElementById('crawler_normalize_actress_name').checked);
            const javdbCookieVal = document.getElementById('network_javdb_cookie').value.trim();
            setPath(cfg, 'network.javdb_cookie', javdbCookieVal || null);

            // metadata complete
            setPath(cfg, 'metadata_complete.enabled', document.getElementById('metadata_enabled').checked);
            setPath(cfg, 'metadata_complete.auto_refresh', document.getElementById('metadata_auto_refresh').checked);
            setPath(cfg, 'metadata_complete.refresh_after', document.getElementById('metadata_refresh_after').value || 'P7D');
            const metadataFields = {};
            document.querySelectorAll('#metadataFieldsBody tr[data-field]').forEach(row => {
                const field = row.dataset.field;
                const lang = row.dataset.lang === 'true';
                const items = row.dataset.items === 'true';
                const reject = row.dataset.reject === 'true';
                const base = defaultMetadataRule(field);
                const required = row.querySelector('[data-key="required"]').checked;
                const entry = {
                    required,
                    prefer_language: null,
                    min_cjk_ratio: 0,
                    min_length: 0,
                    min_items: 0,
                    reject_values: [],
                    allow_overwrite: base.allow_overwrite || field === 'title' || field === 'plot'
                };
                if (lang) {
                    const preferZh = row.querySelector('[data-key="prefer_zh"]')?.checked || false;
                    entry.prefer_language = preferZh ? 'zh' : null;
                    entry.min_cjk_ratio = parseFloat(row.querySelector('[data-key="min_cjk_ratio"]')?.value) || 0;
                    entry.min_length = parseInt(row.querySelector('[data-key="min_length"]')?.value) || 0;
                }
                if (items) {
                    entry.min_items = parseInt(row.querySelector('[data-key="min_items"]')?.value) || 0;
                }
                if (reject) {
                    entry.reject_values = (row.querySelector('[data-key="reject_values"]')?.value || '')
                        .split(',').map(s => s.trim()).filter(Boolean);
                }
                metadataFields[field] = entry;
            });
            setPath(cfg, 'metadata_complete.fields', metadataFields);

            // summarizer
            setPath(cfg, 'summarizer.move_files', document.getElementById('summarizer_move_files').checked);
            setPath(cfg, 'summarizer.path.output_folder_pattern', document.getElementById('summarizer_path_output_folder_pattern').value);
            setPath(cfg, 'summarizer.path.basename_pattern', document.getElementById('summarizer_path_basename_pattern').value);
            setPath(cfg, 'summarizer.path.hard_link', document.getElementById('summarizer_path_hard_link').checked);
            setPath(cfg, 'summarizer.title.remove_trailing_actor_name', document.getElementById('summarizer_title_remove_trailing_actor_name').checked);
            setPath(cfg, 'summarizer.default.title', document.getElementById('summarizer_default_title').value);
            setPath(cfg, 'summarizer.default.actress', document.getElementById('summarizer_default_actress').value);
            setPath(cfg, 'summarizer.nfo.title_pattern', document.getElementById('summarizer_nfo_title_pattern').value);
            setPath(cfg, 'summarizer.nfo.include_actor_tmdbid', document.getElementById('summarizer_nfo_include_actor_tmdbid').checked);
            setPath(cfg, 'summarizer.censor_options_representation', [
                document.getElementById('summarizer_censor_0').value || '无码',
                document.getElementById('summarizer_censor_1').value || '有码',
                document.getElementById('summarizer_censor_2').value || '打码情况未知'
            ]);
            setPath(cfg, 'summarizer.cover.highres', document.getElementById('summarizer_cover_highres').checked);
            setPath(cfg, 'summarizer.cover.add_label', document.getElementById('summarizer_cover_add_label').checked);
            setPath(cfg, 'summarizer.fanart.basename_pattern', document.getElementById('summarizer_fanart_basename_pattern').value);
            setPath(cfg, 'summarizer.extra_fanarts.enabled', document.getElementById('summarizer_extra_fanarts_enabled').checked);
            setPath(cfg, 'summarizer.extra_fanarts.scrap_interval', document.getElementById('summarizer_extra_fanarts_scrap_interval').value || 'PT1.5S');

            // translator
            const engineName = document.getElementById('translator_engine').value;
            if (!engineName) {
                setPath(cfg, 'translator.engine', null);
            } else {
                const engineObj = { name: engineName };
                if (engineName === 'openai') {
                    engineObj.url = document.getElementById('translator_openai_url').value || 'https://api.groq.com/openai/v1/chat/completions';
                    engineObj.api_key = document.getElementById('translator_openai_api_key').value;
                    engineObj.model = document.getElementById('translator_openai_model').value || 'llama-3.1-70b-versatile';
                    const sysPrompt = document.getElementById('translator_openai_system_prompt').value.trim();
                    if (sysPrompt) engineObj.system_prompt = sysPrompt;
                }
                setPath(cfg, 'translator.engine', engineObj);
            }
            setPath(cfg, 'translator.fields.title', document.getElementById('translator_fields_title').checked);
            setPath(cfg, 'translator.fields.plot', document.getElementById('translator_fields_plot').checked);

            // scheduler
            const sched = _schedulerData.scheduler || {};
            sched.enabled = document.getElementById('scheduler_enabled').checked;
            sched.mode = document.getElementById('scheduler_mode').value;
            if (sched.mode === 'cron') {
                sched.cron = { time: document.getElementById('scheduler_cron_time').value || '03:00' };
                delete sched.interval;
            } else {
                sched.interval = {
                    hours: parseInt(document.getElementById('scheduler_interval_hours').value) || 0,
                    minutes: parseInt(document.getElementById('scheduler_interval_minutes').value) || 0
                };
                delete sched.cron;
            }
            _schedulerData.scheduler = sched;

            // refresh scheduler
            const rsched = _schedulerData.refresh_scheduler || {};
            rsched.enabled = document.getElementById('refresh_scheduler_enabled').checked;
            rsched.mode = document.getElementById('refresh_scheduler_mode').value;
            if (rsched.mode === 'cron') {
                rsched.cron = { time: document.getElementById('refresh_scheduler_cron_time').value || '04:00' };
                delete rsched.interval;
            } else {
                rsched.interval = {
                    hours: parseInt(document.getElementById('refresh_scheduler_interval_hours').value) || 0,
                    minutes: parseInt(document.getElementById('refresh_scheduler_interval_minutes').value) || 0
                };
                delete rsched.cron;
            }
            _schedulerData.refresh_scheduler = rsched;
        }

        async function saveCurrentSection() {
            collectFormData();
            try {
                let generalYaml = jsyaml.dump(_configData, {lineWidth: -1, noRefs: true, sortKeys: false});
                // 修复 YAML 1.1 兼容性：Python 的 PyYAML 会把 yes/no 当作布尔值
                generalYaml = generalYaml.replace(/^(\s*use_javdb_cover:\s*)(yes|no)$/gm, '$1"$2"');
                const gRes = await fetch('/api/general_config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({content: generalYaml})
                });
                if (!gRes.ok) {
                    const gData = await gRes.json();
                    showToast('主配置保存失败：' + gData.message, 'error');
                    return;
                }

                const schedulerYaml = jsyaml.dump(_schedulerData, {lineWidth: -1, noRefs: true, sortKeys: false});
                const sRes = await fetch('/api/scheduler_config', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({content: schedulerYaml})
                });
                if (!sRes.ok) {
                    const sData = await sRes.json();
                    showToast('定时配置保存失败：' + sData.message, 'error');
                    return;
                }

                const gData = await gRes.json().catch(() => ({}));
                showToast(gData.message || '配置已保存！');
                loadTaskCenter();
                refreshMovieListFromTasks({scope: _movieListScope});
                loadTaskDashboard();
            } catch (e) {
                showToast('保存失败：' + e.message, 'error');
            }
        }

        async function loadConfigs() {
            try {
                const [res1, res2] = await Promise.all([
                    fetch('/api/scheduler_config').then(r => r.json()),
                    fetch('/api/general_config').then(r => r.json())
                ]);

                // YAML 文本（用于高级模式）
                const schedTextarea = document.getElementById('schedulerConfig');
                const genTextarea = document.getElementById('generalConfig');
                if (schedTextarea) schedTextarea.value = res1.content;
                if (genTextarea) genTextarea.value = res2.content;

                // 解析为对象（用于表单模式）
                try {
                    _schedulerData = jsyaml.load(res1.content) || {};
                } catch(e) { _schedulerData = {}; console.warn('Scheduler YAML parse error:', e); }
                try {
                    _configData = jsyaml.load(res2.content) || {};
                } catch(e) { _configData = {}; console.warn('General YAML parse error:', e); }

                fillForm();
            } catch (e) {
                showToast('无法加载配置文件', 'error');
            }
        }

        async function saveConfig(type) {
            const id = type === 'scheduler' ? 'schedulerConfig' : 'generalConfig';
            const endpoint = type === 'scheduler' ? '/api/scheduler_config' : '/api/general_config';
            const content = document.getElementById(id).value;
            try {
                const res = await fetch(endpoint, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({content})
                });
                const data = await res.json();
                if (res.ok) {
                    showToast(data.message || '保存成功！');
                    const otherId = type === 'scheduler' ? 'generalConfig' : 'schedulerConfig';
                    const otherContent = document.getElementById(otherId).value;
                    await loadConfigs();
                    if (document.getElementById('yamlView').style.display !== 'none') {
                        document.getElementById(id).value = content;
                        document.getElementById(otherId).value = otherContent;
                    }
                    if (type === 'general') {
                        loadTaskCenter();
                        refreshMovieListFromTasks({scope: _movieListScope});
                        loadTaskDashboard();
                    }
                }
                else showToast('保存失败：' + data.message, 'error');
            } catch (e) {
                showToast('网络错误，请稍后重试', 'error');
            }
        }

        // --- 爬虫管理 ---
        let _availableCrawlers = [];
        let _currentSelection = {};
        let _currentFieldPriorities = {title: [], plot: [], actress: [], preview_pics: []};
        const FIELD_PRIORITY_LABELS = {
            title: '标题 title',
            plot: '简介 plot',
            actress: '女优 actress',
            preview_pics: '剧照 preview_pics'
        };

        async function loadCrawlerConfig() {
            try {
                const res = await fetch('/api/crawlers');
                const data = await res.json();
                _availableCrawlers = data.available || [];
                _currentSelection = data.selection || {};
                _currentFieldPriorities = Object.assign({title: [], plot: [], actress: [], preview_pics: []}, data.field_priorities || {});
                renderCrawlerManager();
            } catch (e) {
                showToast('无法加载爬虫配置', 'error');
            }
        }

        function renderCrawlerManager() {
            const poolEl = document.getElementById('crawlerPool');
            const zones = ['normal', 'fc2', 'cid'];
            const used = new Set();
            zones.forEach(z => {
                (_currentSelection[z] || []).forEach(c => used.add(c));
            });
            const unused = _availableCrawlers.filter(c => !used.has(c));
            poolEl.innerHTML = '';
            unused.forEach(name => {
                const tag = document.createElement('div');
                tag.className = 'crawler-tag';
                tag.draggable = true;
                tag.dataset.name = name;
                tag.innerHTML = `<span>${name}</span><button class="add-btn" onclick="addCrawlerToZone('${name}', 'normal')">+</button>`;
                bindDragEvents(tag, 'pool');
                poolEl.appendChild(tag);
            });
            zones.forEach(z => {
                const zoneEl = document.getElementById('zone-' + z);
                zoneEl.innerHTML = '';
                (_currentSelection[z] || []).forEach((name, idx) => {
                    const tag = document.createElement('div');
                    tag.className = 'zone-tag';
                    tag.draggable = true;
                    tag.dataset.name = name;
                    tag.dataset.zone = z;
                    tag.dataset.index = idx;
                    tag.innerHTML = `<span>${name}</span><button class="remove-btn" onclick="removeCrawlerFromZone('${name}', '${z}')">×</button>`;
                    bindDragEvents(tag, 'zone');
                    zoneEl.appendChild(tag);
                });
                updateZoneEmptyState(z);
                document.getElementById('count-' + z).textContent = (_currentSelection[z] || []).length;
            });
            renderFieldPriorityManager();
        }

        function renderFieldPriorityManager() {
            const grid = document.getElementById('fieldPriorityGrid');
            if (!grid) return;
            grid.innerHTML = Object.entries(FIELD_PRIORITY_LABELS).map(([field, label]) => {
                const selected = _currentFieldPriorities[field] || [];
                const options = _availableCrawlers
                    .filter(name => !selected.includes(name))
                    .map(name => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`)
                    .join('');
                const chips = selected.length ? selected.map((name, idx) => `
                    <div class="field-priority-chip">
                        <span class="priority-index">${idx + 1}</span>
                        <span class="priority-name">${escapeHtml(name)}</span>
                        <button onclick="moveFieldPriority('${field}', '${escapeHtml(name)}', -1)" ${idx === 0 ? 'disabled' : ''}>↑</button>
                        <button onclick="moveFieldPriority('${field}', '${escapeHtml(name)}', 1)" ${idx === selected.length - 1 ? 'disabled' : ''}>↓</button>
                        <button onclick="removeFieldPriority('${field}', '${escapeHtml(name)}')">×</button>
                    </div>
                `).join('') : '<div class="field-priority-empty">使用类型顺序</div>';
                return `
                    <div class="field-priority-card">
                        <h4>${escapeHtml(label)}</h4>
                        <div class="field-priority-list">${chips}</div>
                        <div class="field-priority-add">
                            <select id="field-priority-select-${field}" class="form-select">
                                <option value="">选择爬虫</option>
                                ${options}
                            </select>
                            <button onclick="addFieldPriority('${field}')">添加</button>
                            <button onclick="clearFieldPriority('${field}')">清空</button>
                        </div>
                    </div>
                `;
            }).join('');
        }

        function addFieldPriority(field) {
            const select = document.getElementById(`field-priority-select-${field}`);
            const name = select ? select.value : '';
            if (!name) return;
            if (!_currentFieldPriorities[field]) _currentFieldPriorities[field] = [];
            if (!_currentFieldPriorities[field].includes(name)) {
                _currentFieldPriorities[field].push(name);
            }
            renderFieldPriorityManager();
        }

        function removeFieldPriority(field, name) {
            _currentFieldPriorities[field] = (_currentFieldPriorities[field] || []).filter(item => item !== name);
            renderFieldPriorityManager();
        }

        function moveFieldPriority(field, name, delta) {
            const arr = _currentFieldPriorities[field] || [];
            const idx = arr.indexOf(name);
            const next = idx + delta;
            if (idx < 0 || next < 0 || next >= arr.length) return;
            [arr[idx], arr[next]] = [arr[next], arr[idx]];
            renderFieldPriorityManager();
        }

        function clearFieldPriority(field) {
            _currentFieldPriorities[field] = [];
            renderFieldPriorityManager();
        }

        function updateZoneEmptyState(zone) {
            const el = document.getElementById('zone-' + zone);
            if ((_currentSelection[zone] || []).length === 0) {
                el.classList.add('empty');
            } else {
                el.classList.remove('empty');
            }
        }

        function addCrawlerToZone(name, zone) {
            if (!_currentSelection[zone]) _currentSelection[zone] = [];
            if (_currentSelection[zone].includes(name)) return;
            _currentSelection[zone].push(name);
            renderCrawlerManager();
        }

        function removeCrawlerFromZone(name, zone) {
            if (!_currentSelection[zone]) return;
            _currentSelection[zone] = _currentSelection[zone].filter(c => c !== name);
            renderCrawlerManager();
        }

        let _dragSource = null;
        let _dragType = null;

        function bindDragEvents(el, type) {
            el.addEventListener('dragstart', (e) => {
                _dragSource = el;
                _dragType = type;
                el.classList.add('dragging');
                e.dataTransfer.effectAllowed = 'move';
                e.dataTransfer.setData('text/plain', el.dataset.name);
            });
            el.addEventListener('dragend', () => {
                el.classList.remove('dragging');
                _dragSource = null;
                _dragType = null;
                document.querySelectorAll('.zone-droparea').forEach(d => d.classList.remove('drag-over'));
            });
        }

        document.querySelectorAll('.zone-droparea').forEach(zone => {
            zone.addEventListener('dragover', (e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                zone.classList.add('drag-over');
            });
            zone.addEventListener('dragleave', () => {
                zone.classList.remove('drag-over');
            });
            zone.addEventListener('drop', (e) => {
                e.preventDefault();
                zone.classList.remove('drag-over');
                const name = e.dataTransfer.getData('text/plain');
                const targetType = zone.dataset.type;
                if (!name || !targetType) return;

                if (_dragType === 'pool') {
                    if (!_currentSelection[targetType]) _currentSelection[targetType] = [];
                    if (!_currentSelection[targetType].includes(name)) {
                        _currentSelection[targetType].push(name);
                    }
                } else if (_dragType === 'zone') {
                    const sourceZone = _dragSource.dataset.zone;
                    const sourceIdx = parseInt(_dragSource.dataset.index);
                    if (sourceZone === targetType) {
                        const arr = _currentSelection[sourceZone];
                        const name = _dragSource.dataset.name;
                        arr.splice(sourceIdx, 1);
                        const dropX = e.clientX;
                        const dropY = e.clientY;
                        const targetEl = document.elementFromPoint(dropX, dropY);
                        let targetIdx = arr.length;
                        if (targetEl && targetEl.dataset.index !== undefined) {
                            const idx = parseInt(targetEl.dataset.index);
                            targetIdx = dropX < targetEl.getBoundingClientRect().left + targetEl.offsetWidth / 2 ? idx : idx + 1;
                        }
                        arr.splice(targetIdx, 0, name);
                    } else {
                        _currentSelection[sourceZone].splice(sourceIdx, 1);
                        if (!_currentSelection[targetType]) _currentSelection[targetType] = [];
                        if (!_currentSelection[targetType].includes(name)) {
                            _currentSelection[targetType].push(name);
                        }
                    }
                }
                renderCrawlerManager();
            });
        });

        document.getElementById('crawlerPool').addEventListener('dragover', (e) => {
            if (_dragType === 'zone') {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
            }
        });
        document.getElementById('crawlerPool').addEventListener('drop', (e) => {
            e.preventDefault();
            if (_dragType === 'zone' && _dragSource) {
                const name = _dragSource.dataset.name;
                const sourceZone = _dragSource.dataset.zone;
                const sourceIdx = parseInt(_dragSource.dataset.index);
                _currentSelection[sourceZone].splice(sourceIdx, 1);
                renderCrawlerManager();
            }
        });

        async function saveCrawlerConfig() {
            try {
                const res = await fetch('/api/crawlers', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({selection: _currentSelection, field_priorities: _currentFieldPriorities})
                });
                const data = await res.json();
                if (res.ok) {
                    showToast('爬虫配置已保存！');
                    loadConfigs();
                }
                else showToast('保存失败：' + data.message, 'error');
            } catch (e) {
                showToast('网络错误，请稍后重试', 'error');
            }
        }

        // --- 爬虫调试功能 ---
        let _debugCrawlers = [];

        async function loadDebugCrawlers() {
            try {
                const res = await fetch('/api/crawlers');
                const data = await res.json();
                _debugCrawlers = data.available || [];
                const sel = document.getElementById('debugCrawlerSelect');
                sel.innerHTML = '<option value="">-- 选择爬虫 --</option>';
                _debugCrawlers.forEach(name => {
                    const opt = document.createElement('option');
                    opt.value = name;
                    opt.textContent = name;
                    sel.appendChild(opt);
                });
            } catch (e) {
                console.error('加载爬虫列表失败:', e);
            }
        }

        async function runCrawlerDebug() {
            const crawlerName = document.getElementById('debugCrawlerSelect').value;
            const dvdid = document.getElementById('debugDvdid').value.trim();
            const cid = document.getElementById('debugCid').value.trim();
            const body = {crawler_name: crawlerName};

            if (!crawlerName) { showToast('请选择爬虫', 'error'); return; }
            if (!dvdid && !cid) { showToast('请填写 DVD ID 或 Content ID', 'error'); return; }
            if (dvdid) body.dvdid = dvdid;
            if (cid) body.cid = cid;

            const runBtn = document.querySelector('.debug-run-btn');
            runBtn.disabled = true;
            runBtn.textContent = '⏳ 抓取中...';
            document.getElementById('debugElapsed').textContent = '';
            document.getElementById('debugResultBody').innerHTML = '<div class="debug-empty">⏳ 正在抓取数据...</div>';

            try {
                const res = await fetch('/api/crawlers/debug', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body)
                });
                const data = await res.json();
                document.getElementById('debugElapsed').textContent = `耗时 ${data.elapsed}s`;
                renderDebugResult(data);
            } catch (e) {
                document.getElementById('debugElapsed').textContent = '';
                document.getElementById('debugResultBody').innerHTML =
                    `<div class="debug-error-box"><div class="error-type">NETWORK ERROR</div>${e.message}</div>`;
            } finally {
                runBtn.disabled = false;
                runBtn.textContent = '▶ 调试运行';
            }
        }

        function renderDebugResult(data) {
            if (data.success) {
                const fields = data.fields || {};
                const keys = Object.keys(fields);
                let html = '';
                html += `<div class="debug-meta-row">`;
                html += `<span class="debug-status-success">✔ 抓取成功</span>`;
                if (data.url) {
                    html += `<span>URL: <a href="${data.url}" target="_blank" class="debug-value-url">${data.url}</a></span>`;
                }
                html += `<span class="debug-meta">DVD ID: <strong>${data.dvdid || '-'}</strong></span>`;
                if (data.cid) html += `<span class="debug-meta">CID: <strong>${data.cid}</strong></span>`;
                html += `<span class="debug-meta">获取字段: <strong>${data.field_count}</strong> / 24</span>`;
                html += `</div>`;

                if (keys.length === 0) {
                    html += '<div class="debug-empty">未获取到任何字段</div>';
                } else {
                    html += '<table class="debug-fields-table"><thead><tr><th>字段</th><th>值</th></tr></thead><tbody>';
                    const fieldLabels = {
                        dvdid: '番号', cid: 'Content ID', url: '影片URL', plot: '剧情简介',
                        cover: '封面URL', big_cover: '高清封面URL', genre: '分类', genre_id: '分类ID',
                        genre_norm: '标准化分类', score: '评分(10分制)', title: '标题', ori_title: '原始标题',
                        magnet: '磁力链接', serial: '系列', actress: '女优', actress_pics: '女优头像',
                        director: '导演', duration: '时长(分钟)', producer: '制作商', publisher: '发行商',
                        uncensored: '无码', publish_date: '发布日期', preview_pics: '预览图', preview_video: '预览视频'
                    };
                    for (const key of keys) {
                        const val = fields[key];
                        const displayVal = formatDebugField(key, val);
                        html += `<tr>
                            <td class="field-name" title="${key}">${fieldLabels[key] || key}</td>
                            <td class="field-value">${displayVal}</td>
                        </tr>`;
                    }
                    html += '</tbody></table>';
                }
                document.getElementById('debugResultBody').innerHTML = html;
            } else {
                let html = `<div class="debug-error-box">`;
                html += `<div class="error-type">${data.type || 'ERROR'} ✘ 抓取失败</div>`;
                html += `<div>${escapeHtml(data.error || '未知错误')}</div>`;
                if (data.traceback) {
                    const tbId = 'tb_' + Date.now();
                    html += `<button class="debug-traceback-toggle" onclick="document.getElementById('${tbId}').style.display = document.getElementById('${tbId}').style.display === 'block' ? 'none' : 'block'">显示/隐藏堆栈跟踪</button>`;
                    html += `<div class="debug-traceback" id="${tbId}">${escapeHtml(data.traceback)}</div>`;
                }
                html += `</div>`;
                document.getElementById('debugResultBody').innerHTML = html;
            }
        }

        function formatDebugField(key, val) {
            if (val === null || val === undefined) return '<span class="empty-value">(空)</span>';
            if (typeof val === 'boolean') return val ? '✔ 是' : '✘ 否';
            if (Array.isArray(val)) {
                if (val.length === 0) return '<span class="empty-value">(空列表)</span>';
                return '<div class="debug-value-list">' + val.map(v => `<span class="debug-value-tag">${escapeHtml(String(v))}</span>`).join('') + '</div>';
            }
            if (typeof val === 'object') {
                const items = Object.entries(val).map(([k, v]) => `<span class="debug-value-tag">${escapeHtml(String(k))}: ${escapeHtml(String(v))}</span>`);
                return '<div class="debug-value-list">' + items.join('') + '</div>';
            }
            const str = String(val);
            if (str.startsWith('http://') || str.startsWith('https://')) {
                return `<a href="${escapeHtml(str)}" target="_blank" class="debug-value-url">${escapeHtml(str)}</a>`;
            }
            return escapeHtml(str);
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        // Checkbox 样式交互
        document.querySelectorAll('.checkbox-item input').forEach(cb => {
            cb.addEventListener('change', () => {
                cb.closest('.checkbox-item').classList.toggle('checked', cb.checked);
            });
        });

        // 初始化加载
        loadConfigs();
        restoreScrapeStatus();
        refreshMovieListFromTasks({scope: 'latest'});
        loadTaskDashboard();
        loadTaskCenter();
