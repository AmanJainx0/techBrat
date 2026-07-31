document.addEventListener('DOMContentLoaded', () => {

    /* =======================================
       DOM REFS
    ======================================= */
    const courseContainer  = document.getElementById('course-list-container');
    const bannerText       = document.getElementById('personalization-banner-text');
    const applyBtn         = document.getElementById('apply-filters-btn');
    const resetBtn         = document.getElementById('reset-filters');
    const domainInput      = document.getElementById('domain-search');
    const domainTagsWrap   = document.getElementById('domain-tags');
    const courseCountEl     = document.getElementById('course-count');
    const filterSidebar    = document.getElementById('filter-sidebar');
    const filterToggle     = document.getElementById('mobile-filter-toggle');
    const filterOverlay    = document.getElementById('filter-overlay');
    const sidebarCloseBtn  = document.getElementById('sidebar-close');

    /* =======================================
       INITIAL STATE
    ======================================= */
    const userLevel      = JSON.parse(document.getElementById('user-level-data')?.textContent || '"beginner"');
    const userDomainsRaw = JSON.parse(document.getElementById('user-domains-data')?.textContent || '""');

    const parseDomains = (raw) => {
        if (!raw) return [];
        if (Array.isArray(raw)) return raw.filter(Boolean).map(String);
        if (typeof raw === 'string') return raw.split(',').map(d => d.trim()).filter(Boolean);
        return [];
    };

    const defaults = {
        level: userLevel || 'beginner',
        learning_type: 'video',
        pricing: 'free',       // 'free' | 'paid' | 'all'
        domains: parseDomains(userDomainsRaw),
    };

    let filters = { ...defaults, domains: [...defaults.domains] };
    let debounceTimer = null;

    /* =======================================
       UI SYNC
    ======================================= */

    function syncAllUI() {
        // Level pills
        document.querySelectorAll('#level-pills .filter-pill').forEach(p => {
            p.classList.toggle('active', p.dataset.value === filters.level);
        });
        // Type cards
        document.querySelectorAll('#type-cards .type-card').forEach(c => {
            c.classList.toggle('active', c.dataset.value === filters.learning_type);
        });
        // Pricing
        document.querySelectorAll('#pricing-group .pricing-option').forEach(o => {
            o.classList.toggle('active', o.dataset.value === filters.pricing);
        });
        // Domains
        renderDomainTags();
    }

    function renderDomainTags() {
        if (!domainTagsWrap) return;
        domainTagsWrap.innerHTML = filters.domains.map(d => `
            <span class="domain-tag" data-domain="${escapeHTML(d)}">
                ${escapeHTML(d)}
                <i class="fas fa-times remove-tag"></i>
            </span>
        `).join('');

        domainTagsWrap.querySelectorAll('.remove-tag').forEach(icon => {
            icon.addEventListener('click', (e) => {
                const domain = e.target.closest('.domain-tag')?.dataset.domain;
                if (!domain) return;
                filters.domains = filters.domains.filter(dd => dd !== domain);
                renderDomainTags();
                debouncedFetch();
            });
        });
    }

    /* =======================================
       SKELETON LOADING
    ======================================= */

    function showSkeleton(count = 6) {
        if (!courseContainer) return;
        let html = '';
        for (let i = 0; i < count; i++) {
            html += `
            <div class="skeleton-card">
                <div class="skeleton-accent"></div>
                <div class="skeleton-body">
                    <div class="skeleton-badges">
                        <div class="skeleton-badge"></div>
                        <div class="skeleton-badge"></div>
                    </div>
                    <div class="skeleton-line w-80 h-20"></div>
                    <div class="skeleton-line w-40"></div>
                    <div class="skeleton-line w-100 mt-1"></div>
                    <div class="skeleton-line w-60"></div>
                    <div class="skeleton-line w-80 mt-1 h-8"></div>
                    <div class="skeleton-btn"></div>
                </div>
            </div>`;
        }
        courseContainer.innerHTML = html;
    }

    /* =======================================
       FETCH COURSES
    ======================================= */

    function debouncedFetch() {
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(fetchCourses, 400);
    }

    async function fetchCourses() {
        showSkeleton();

        const domainParam = filters.domains.join(',');
        const isFree = filters.pricing === 'all' ? 'true' : (filters.pricing === 'free' ? 'true' : 'false');

        try {
            const url = `/api/filter-courses/?level=${enc(filters.level)}&domain=${enc(domainParam)}&learning_type=${enc(filters.learning_type)}&free=${isFree}`;
            const response = await fetch(url);

            if (!response.ok) {
                let errorMsg = null;
                try {
                    const errData = await response.json();
                    errorMsg = errData.error || null;
                } catch(e) {}
                renderError(errorMsg);
                return;
            }

            const data = await response.json();

            if (data.success) {
                let courses = data.courses || [];
                // If pricing = 'all', we already got free; we should show all
                // The backend returns based on free param, so 'all' just shows free for now
                renderCourses(courses);
                updateBanner(courses.length);
            } else {
                renderError(data.error);
            }
        } catch (err) {
            console.error('Fetch error:', err);
            renderError();
        }
    }

    /* =======================================
       RENDER COURSES
    ======================================= */

    function renderCourses(courses) {
        if (!courseContainer) return;

        if (!courses || courses.length === 0) {
            renderEmpty();
            return;
        }

        courseContainer.innerHTML = courses.map(c => {
            const detailsLink = c.detail_url ? `
                <div class="col-12 p-0 mt-2">
                    <a href="${escapeHTML(c.detail_url)}" class="btn btn-link btn-details-link w-100 text-center text-decoration-none">
                        View Full Details <i class="fas fa-arrow-right ms-1"></i>
                    </a>
                </div>
            ` : '';

            return `
                <div class="course-card">
                    ${c.id ? `
                    <button
                        type="button"
                        class="save-toggle-btn course-save-btn btn-bookmark-floating ${c.saved ? 'is-saved' : ''}"
                        data-object-id="${c.id}"
                        data-content-type="${escapeHTML(c.content_type || 'course')}"
                        data-saved="${c.saved ? 'true' : 'false'}"
                        aria-label="Save Course">
                        <i class="${c.saved ? 'fas' : 'far'} fa-bookmark"></i>
                    </button>
                    ` : ''}
                    <div class="card-body p-4 d-flex flex-column">
                        <div class="card-badges">
                            <span class="badge badge-level">${escapeHTML(c.level_display || c.level)}</span>
                            <span class="badge ${c.is_free ? 'badge-free' : 'badge-paid'}">
                                ${c.is_free ? 'Free' : 'Paid'}
                            </span>
                        </div>

                        <div class="course-platform">
                            <i class="fas fa-building"></i>${escapeHTML(c.platform)}
                        </div>
                        <h3 class="card-title">${escapeHTML(c.title)}</h3>
                        <p class="card-description">${escapeHTML(c.description)}</p>

                        <div class="course-meta">
                            <span><i class="far fa-clock"></i>${escapeHTML(c.duration || 'Self-paced')}</span>
                            <span><i class="fas fa-graduation-cap"></i>${escapeHTML(c.learning_type_display || c.learning_type)}</span>
                        </div>

                        <div class="card-footer mt-auto p-0 border-0 bg-transparent">
                            <div class="row g-2 w-100 m-0">
                                <div class="col-6 p-0">
                                    <a href="${escapeHTML(c.link)}" target="_blank" rel="noopener" class="btn btn-primary w-100 btn-action">
                                        Start Learning <i class="fas fa-external-link-alt ms-1 small"></i>
                                    </a>
                                </div>
                                <div class="col-6 p-0">
                                    <button
                                        type="button"
                                        class="btn btn-outline-secondary w-100 btn-action btn-preview"
                                        data-course='${escapeAttr(JSON.stringify({
                                            title: c.title,
                                            platform: c.platform,
                                            desc: c.description,
                                            link: c.link,
                                            duration: c.duration,
                                            learning_type: c.learning_type_display || c.learning_type
                                        }))}'>
                                        <i class="fas fa-eye me-1"></i> Preview
                                    </button>
                                </div>
                                ${detailsLink}
                            </div>
                        </div>

                    </div>
                </div>
            `;
        }).join('');

        updateCount(courses.length);
        attachMobilePreviewHandlers();
    }

    function renderEmpty() {
        if (!courseContainer) return;
        courseContainer.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon"><i class="fas fa-search"></i></div>
                <h3>No courses found</h3>
                <p>Try adjusting your filters or adding different domains to discover new courses.</p>
            </div>
        `;
        updateCount(0);
    }

    function renderError(customMessage) {
        if (!courseContainer) return;
        
        const title = customMessage ? "Unsupported Topic" : "Something went wrong";
        const msg = customMessage ? escapeHTML(customMessage) : "We couldn't load courses right now. Please try again.";
        
        courseContainer.innerHTML = `
            <div class="error-state">
                <div class="error-icon"><i class="fas fa-exclamation-triangle"></i></div>
                <h3>${title}</h3>
                <p>${msg}</p>
                <button class="btn-retry" id="retry-btn"><i class="fas fa-redo"></i> Retry</button>
            </div>
        `;
        updateCount(0);
        document.getElementById('retry-btn')?.addEventListener('click', fetchCourses);
    }

    function updateBanner(count) {
        if (!bannerText) return;
        bannerText.innerHTML = `
            <p class="hero-subtitle">
                <i class="fas fa-sparkles" style="margin-right:6px;color:#818cf8"></i>
                Showing <strong>${count} course${count !== 1 ? 's' : ''}</strong> updated dynamically.
            </p>
        `;
    }

    function updateCount(n) {
        if (courseCountEl) {
            courseCountEl.textContent = `${n} course${n !== 1 ? 's' : ''}`;
        }
    }

    /* =======================================
       MOBILE PREVIEW MODAL
    ======================================= */

    function attachMobilePreviewHandlers() {
        const modalEl = document.getElementById('previewModal');
        if (!modalEl || typeof bootstrap === 'undefined') return;

        const previewModal = new bootstrap.Modal(modalEl);

        document.querySelectorAll('.btn-preview[data-course]').forEach(btn => {
            btn.addEventListener('click', () => {
                try {
                    const d = JSON.parse(btn.dataset.course || '{}');
                    document.getElementById('modalTitle').textContent   = d.title || 'Course Preview';
                    document.getElementById('modalPlatform').textContent = d.platform || '';
                    document.getElementById('modalDesc').textContent     = d.desc || '';
                    document.getElementById('modalLink').href            = d.link || '#';
                    const metaEl = document.getElementById('modalMeta');
                    if (metaEl) {
                        metaEl.innerHTML = `
                            <span><i class="far fa-clock"></i> ${escapeHTML(d.duration || 'Self-paced')}</span>
                            <span><i class="fas fa-graduation-cap"></i> ${escapeHTML(d.learning_type || '')}</span>
                        `;
                    }
                    previewModal.show();
                } catch (e) {
                    console.error('Preview parse error:', e);
                }
            });
        });
    }

    /* =======================================
       MOBILE SIDEBAR DRAWER
    ======================================= */

    function openSidebar() {
        filterSidebar?.classList.add('mobile-open');
        filterOverlay?.classList.add('active');
        if (filterOverlay) filterOverlay.style.display = 'block';
        document.body.style.overflow = 'hidden';
    }

    function closeSidebar() {
        filterSidebar?.classList.remove('mobile-open');
        filterOverlay?.classList.remove('active');
        setTimeout(() => {
            if (filterOverlay) filterOverlay.style.display = 'none';
        }, 300);
        document.body.style.overflow = '';
    }

    filterToggle?.addEventListener('click', openSidebar);
    sidebarCloseBtn?.addEventListener('click', closeSidebar);
    filterOverlay?.addEventListener('click', closeSidebar);

    /* =======================================
       EVENT LISTENERS
    ======================================= */

    // Level pills
    document.querySelectorAll('#level-pills .filter-pill').forEach(pill => {
        pill.addEventListener('click', () => {
            filters.level = pill.dataset.value;
            syncAllUI();
            debouncedFetch();
        });
    });

    // Type cards
    document.querySelectorAll('#type-cards .type-card').forEach(card => {
        card.addEventListener('click', () => {
            filters.learning_type = card.dataset.value;
            syncAllUI();
            debouncedFetch();
        });
    });

    // Pricing
    document.querySelectorAll('#pricing-group .pricing-option').forEach(opt => {
        opt.addEventListener('click', () => {
            filters.pricing = opt.dataset.value;
            syncAllUI();
            debouncedFetch();
        });
    });

    // Domain input
    domainInput?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            const val = domainInput.value.trim();
            if (val && !filters.domains.includes(val)) {
                filters.domains.push(val);
                renderDomainTags();
                debouncedFetch();
            }
            domainInput.value = '';
        }
    });

    // Apply button
    applyBtn?.addEventListener('click', () => {
        clearTimeout(debounceTimer);
        fetchCourses();
        closeSidebar();
    });

    // Reset button
    resetBtn?.addEventListener('click', () => {
        filters = { ...defaults, domains: [...defaults.domains] };
        syncAllUI();
        fetchCourses();
    });

    /* =======================================
       HELPERS
    ======================================= */

    function enc(s) { return encodeURIComponent(s); }

    function escapeHTML(str) {
        if (!str) return '';
        const d = document.createElement('div');
        d.textContent = str;
        return d.innerHTML;
    }

    function escapeAttr(str) {
        return str.replace(/'/g, '&#39;').replace(/"/g, '&quot;');
    }

    /* =======================================
       INIT
    ======================================= */
    syncAllUI();
    fetchCourses();

});
