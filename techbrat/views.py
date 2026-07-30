import hashlib
import json
import os
import re
from datetime import timedelta
from urllib.parse import urlsplit

from django.shortcuts import get_object_or_404, render, redirect
from django.urls import reverse
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.conf import settings
from django.contrib.sites.models import Site
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from utils.ai_validator import is_tech_query, normalize_tech_query, get_alternative_suggestions
from utils.opportunity_sources import SOURCE_LABELS, fetch_live_opportunities
from utils.openrouter_client import post_openrouter

from techbrat.models import (
    Book,
    CareerPathSnapshot,
    CompanyPrepSnapshot,
    Course,
    IssueAssistanceSnapshot,
    LearningActivity,
    RoadmapStepProgress,
    RoadmapSnapshot,
    SavedItem,
    Tip,
    Tool,
    UserProfile,
    UserSkill,
    WeeklyGoal,
)


def _get_safe_next_url(request, default=''):
    candidates = [
        request.POST.get('next'),
        request.GET.get('next'),
    ]
    allowed_hosts = {
        request.get_host(),
        request.get_host().split(':', 1)[0],
    }
    blocked_paths = {
        reverse('techbrat:signin'),
        reverse('techbrat:signin').rstrip('/'),
        reverse('techbrat:signup'),
        reverse('techbrat:signup').rstrip('/'),
        reverse('techbrat:index'),
        reverse('techbrat:index').rstrip('/'),
        '/',
    }

    for candidate in candidates:
        if not candidate:
            continue
        if not url_has_allowed_host_and_scheme(
            candidate,
            allowed_hosts=allowed_hosts,
            require_https=request.is_secure(),
        ):
            continue

        candidate_path = urlsplit(candidate).path
        if candidate_path in blocked_paths:
            continue

        return candidate

    return default


def get_landing_context(request=None, auth_mode='signup'):
    try:
        from allauth.socialaccount.models import SocialApp

        site = Site.objects.get_current()
        request_host = request.get_host().split(':', 1)[0] if request else ''
        site_domain_raw = (site.domain or '').strip().lower()
        site_domain = site_domain_raw.split(':', 1)[0]
        local_aliases = {'127.0.0.1', 'localhost'}
        host_matches_site = bool(request_host) and (
            site_domain == request_host.lower()
            or (
                site_domain in local_aliases
                and request_host.lower() in local_aliases
            )
        )
        site_is_placeholder = site_domain in {'', 'example.com'}

        google_enabled = (
            SocialApp.objects.filter(provider='google', sites=site).exists()
            and host_matches_site
            and not site_is_placeholder
        )
        github_enabled = (
            SocialApp.objects.filter(provider='github', sites=site).exists()
            and host_matches_site
            and not site_is_placeholder
        )
    except Exception:
        google_enabled = bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)
        github_enabled = bool(settings.GITHUB_CLIENT_ID and settings.GITHUB_CLIENT_SECRET)
    print("Request Host:", request_host)
    print("Site Domain:", site_domain)
    print("Host Matches:", host_matches_site)
    print("Google Exists:", SocialApp.objects.filter(provider="google", sites=site).exists())
    print("Google Enabled:", google_enabled)
    return {
        'google_login_enabled': google_enabled,
        'github_login_enabled': github_enabled,
        'auth_mode': auth_mode,
        'next_url': _get_safe_next_url(request) if request else '',
    }


def _user_cache_key(prefix, user):
    return f"{prefix}_{user.pk}"


SAVEABLE_MODELS = {
    'careerpathsnapshot': CareerPathSnapshot,
    'companyprepsnapshot': CompanyPrepSnapshot,
    'course': Course,
    'book': Book,
    'issueassistancesnapshot': IssueAssistanceSnapshot,
    'roadmapsnapshot': RoadmapSnapshot,
    'tool': Tool,
    'tip': Tip,
}


def _get_saveable_model(model_name):
    return SAVEABLE_MODELS.get((model_name or '').strip().lower())


def _saved_lookup_for_user(user):
    if not user.is_authenticated:
        return set()

    return set(
        SavedItem.objects.filter(user=user).values_list('content_type_id', 'object_id')
    )


def _saved_payload_for_object(obj, saved_lookup):
    content_type = ContentType.objects.get_for_model(obj.__class__)
    return {
        'id': obj.pk,
        'content_type': content_type.model,
        'saved': (content_type.id, obj.pk) in saved_lookup,
        'detail_url': obj.get_absolute_url(),
    }


def _normalize_roadmap_step_title(title, index):
    raw_title = str(title or '').strip()
    if not raw_title:
        return f'Step {index + 1}'

    normalized = re.sub(r'^(?:\s*Step\s*\d+\s*[:\-–]\s*)+', '', raw_title, flags=re.I).strip()
    return normalized or f'Step {index + 1}'


def _roadmap_steps_with_progress(snapshot):
    roadmap = snapshot.payload.get('roadmap', {})
    steps = roadmap.get('steps', []) if isinstance(roadmap, dict) else []
    completed_indexes = set(
        snapshot.step_progress.values_list('step_index', flat=True)
    )

    normalized_steps = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        normalized_steps.append({
            'index': index,
            'title': _normalize_roadmap_step_title(step.get('title', ''), index),
            'description': step.get('description', ''),
            'resources': step.get('resources', []),
            'is_completed': index in completed_indexes,
        })
    return normalized_steps


def _roadmap_dashboard_cards(user):
    cache_key = _user_cache_key('roadmap_dashboard_cards', user)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    snapshots = (
        RoadmapSnapshot.objects.filter(user=user)
        .prefetch_related('step_progress')
        .order_by('-created_at')
    )
    cards = []
    for snapshot in snapshots:
        steps = _roadmap_steps_with_progress(snapshot)
        total_steps = len(steps)
        completed_steps = sum(1 for step in steps if step['is_completed'])
        cards.append({
            'snapshot': snapshot,
            'steps': steps,
            'total_steps': total_steps,
            'completed_steps': completed_steps,
            'remaining_steps': max(total_steps - completed_steps, 0),
            'completion_percentage': round((completed_steps / total_steps) * 100) if total_steps else 0,
            'next_step': next((step for step in steps if not step['is_completed']), None),
        })
    cache.set(cache_key, cards, 60)
    return cards


def _current_week_start():
    today = timezone.localdate()
    return today - timedelta(days=today.weekday())


def _weekly_goal_summary(user):
    cache_key = _user_cache_key('weekly_goal_summary', user)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    week_start = _current_week_start()
    week_end = week_start + timedelta(days=6)
    goal, _ = WeeklyGoal.objects.get_or_create(
        user=user,
        week_start=week_start,
        defaults={'target_steps': 5},
    )
    completed_this_week = LearningActivity.objects.filter(
        user=user,
        activity_type='roadmap_step_completed',
        activity_date__range=(week_start, week_end),
    ).count()
    completion_percentage = round((completed_this_week / goal.target_steps) * 100) if goal.target_steps else 0
    summary = {
        'goal': goal,
        'week_start': week_start,
        'week_end': week_end,
        'completed_this_week': completed_this_week,
        'remaining_steps': max(goal.target_steps - completed_this_week, 0),
        'completion_percentage': min(completion_percentage, 100),
    }
    cache.set(cache_key, summary, 60)
    return summary


def _current_streak_days(user):
    cache_key = _user_cache_key('current_streak_days', user)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    activity_dates = set(
        LearningActivity.objects.filter(
            user=user,
            activity_type='roadmap_step_completed',
        ).values_list('activity_date', flat=True)
    )
    if not activity_dates:
        cache.set(cache_key, 0, 60)
        return 0

    today = timezone.localdate()
    current_day = today if today in activity_dates else today - timedelta(days=1)
    if current_day not in activity_dates:
        cache.set(cache_key, 0, 60)
        return 0

    streak = 0
    while current_day in activity_dates:
        streak += 1
        current_day -= timedelta(days=1)
    cache.set(cache_key, streak, 60)
    return streak


def _profile_completion_details(profile):
    skills_count = profile.skills.count()
    checks = [
        {
            'label': 'Current course',
            'status': bool(profile.current_course),
            'detail': profile.current_course or 'Add your current course or degree.',
        },
        {
            'label': 'Year of study',
            'status': bool(profile.year_of_study),
            'detail': profile.year_of_study or 'Add your current year or stage.',
        },
        {
            'label': 'Target domains',
            'status': bool(profile.tech_domains),
            'detail': profile.tech_domains or 'Add the tech domains you want to grow in.',
        },
        {
            'label': 'Target timeline',
            'status': bool(profile.target_timeline),
            'detail': profile.target_timeline or 'Set a realistic timeline for your next goal.',
        },
        {
            'label': 'Skills added',
            'status': skills_count > 0,
            'detail': f'{skills_count} skill{"s" if skills_count != 1 else ""} added' if skills_count else 'Add your current skills and proficiency levels.',
        },
    ]
    return checks


def _recent_activity_snapshot(user):
    today = timezone.localdate()
    week_start = today - timedelta(days=6)
    recent_activity = LearningActivity.objects.filter(
        user=user,
        activity_type='roadmap_step_completed',
        activity_date__range=(week_start, today),
    )
    active_days = recent_activity.values('activity_date').distinct().count()
    completed_steps_last_7_days = recent_activity.count()
    return {
        'active_days': active_days,
        'completed_steps_last_7_days': completed_steps_last_7_days,
        'window_label': f'{week_start.strftime("%b %d")} - {today.strftime("%b %d")}',
    }


def _progress_overview_summary(user, roadmap_cards, weekly_goal, streak_days, readiness, skill_gap):
    total_steps = sum(card['total_steps'] for card in roadmap_cards)
    completed_steps = sum(card['completed_steps'] for card in roadmap_cards)
    remaining_steps = max(total_steps - completed_steps, 0)
    overall_completion = round((completed_steps / total_steps) * 100) if total_steps else 0
    recent_activity = _recent_activity_snapshot(user)

    if readiness['score'] >= 80:
        momentum_label = 'Strong momentum'
        momentum_summary = 'You are building real evidence that you can learn consistently and move toward interviews with confidence.'
    elif readiness['score'] >= 55:
        momentum_label = 'Solid progress'
        momentum_summary = 'Your direction is good. The biggest win now is turning partial progress into stronger proof of execution.'
    else:
        momentum_label = 'Foundation stage'
        momentum_summary = 'You have a starting base, and the fastest improvement will come from tightening your profile, skills, and roadmap habits.'

    if roadmap_cards:
        top_roadmap = max(roadmap_cards, key=lambda card: (card['completion_percentage'], card['completed_steps']))
        spotlight = (
            f"Your strongest roadmap right now is '{top_roadmap['snapshot'].title}' at "
            f"{top_roadmap['completion_percentage']}% completion."
        )
    else:
        spotlight = 'You have not saved a roadmap yet, so the best next move is to generate one and start tracking progress.'

    if weekly_goal['remaining_steps'] > 0:
        next_win = (
            f"Finish {weekly_goal['remaining_steps']} more roadmap step"
            f"{'s' if weekly_goal['remaining_steps'] != 1 else ''} this week to hit your target."
        )
    else:
        next_win = 'You have already hit this week\'s target, so use the extra time to revise and strengthen one proof-of-work project.'

    quick_facts = [
        {
            'label': 'Track fit',
            'value': skill_gap['track_label'],
            'detail': skill_gap['simple_summary'],
        },
        {
            'label': 'Recent learning',
            'value': f"{recent_activity['completed_steps_last_7_days']} step{'s' if recent_activity['completed_steps_last_7_days'] != 1 else ''}",
            'detail': f"Across {recent_activity['active_days']} active day{'s' if recent_activity['active_days'] != 1 else ''} in the last 7 days",
        },
        {
            'label': 'Remaining roadmap work',
            'value': remaining_steps,
            'detail': f'{completed_steps} of {total_steps} total steps completed' if total_steps else 'No roadmap steps tracked yet',
        },
        {
            'label': 'Streak health',
            'value': f'{streak_days} day{"s" if streak_days != 1 else ""}',
            'detail': 'Consistency builds faster readiness gains than random bursts.',
        },
    ]

    return {
        'momentum_label': momentum_label,
        'momentum_summary': momentum_summary,
        'overall_completion': overall_completion,
        'remaining_steps': remaining_steps,
        'spotlight': spotlight,
        'next_win': next_win,
        'quick_facts': quick_facts,
        'recent_activity': recent_activity,
    }


def _job_readiness_summary(user):
    cache_key = _user_cache_key('job_readiness_summary', user)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    profile = get_or_create_profile(user)
    profile_completion = get_profile_completion(profile)
    skills = list(profile.skills.all())
    roadmap_cards = _roadmap_dashboard_cards(user)
    weekly_goal = _weekly_goal_summary(user)
    streak_days = _current_streak_days(user)
    profile_checks = _profile_completion_details(profile)

    skill_score = min(len(skills) * 10, 30)
    advanced_bonus = min(sum(1 for skill in skills if skill.proficiency == 'advanced') * 5, 10)
    roadmap_total = sum(card['total_steps'] for card in roadmap_cards)
    roadmap_completed = sum(card['completed_steps'] for card in roadmap_cards)
    roadmap_score = round((roadmap_completed / roadmap_total) * 30) if roadmap_total else 0
    consistency_score = min(weekly_goal['completed_this_week'] * 4, 20)
    streak_bonus = min(streak_days * 2, 10)

    readiness_score = min(
        round((profile_completion * 0.3) + skill_score + advanced_bonus + roadmap_score + consistency_score + streak_bonus),
        100,
    )

    if readiness_score >= 80:
        readiness_label = 'Strong'
    elif readiness_score >= 55:
        readiness_label = 'Building'
    else:
        readiness_label = 'Early Stage'

    next_label = None
    next_threshold = None
    if readiness_score < 55:
        next_label = 'Building'
        next_threshold = 55
    elif readiness_score < 80:
        next_label = 'Strong'
        next_threshold = 80

    strengths = []
    if profile_completion >= 80:
        strengths.append('Your profile has enough detail to make guidance more personalized.')
    if len(skills) >= 3:
        strengths.append(f'You already have {len(skills)} declared skill{"s" if len(skills) != 1 else ""}, which gives you a visible foundation.')
    if roadmap_completed > 0:
        strengths.append(f'You have already finished {roadmap_completed} roadmap step{"s" if roadmap_completed != 1 else ""}.')
    if streak_days >= 3:
        strengths.append(f'Your {streak_days}-day streak shows learning consistency.')

    blockers = []
    if profile_completion < 80:
        blockers.append('Complete more profile details to sharpen recommendations and planning.')
    if len(skills) < 3:
        blockers.append('Add and develop at least 3 meaningful skills in your profile.')
    if roadmap_total == 0:
        blockers.append('Save a roadmap so your learning path becomes trackable.')
    elif roadmap_completed < max(1, roadmap_total // 3):
        blockers.append('Complete more roadmap steps to move from planning into execution.')
    if weekly_goal['completed_this_week'] == 0:
        blockers.append('Finish at least one roadmap step this week to build momentum.')
    if streak_days < 3:
        blockers.append('Build a 3-day learning streak to make progress consistent.')

    components = [
        {
            'label': 'Profile clarity',
            'score': round(profile_completion * 0.3),
            'max_score': 30,
            'display_value': f'{profile_completion}%',
            'status': 'Strong' if profile_completion >= 80 else 'Needs attention',
            'explanation': 'This shows how complete your profile information is.',
            'target': 'Aim for 80%+ so recommendations can be more specific.',
        },
        {
            'label': 'Skill foundation',
            'score': min(skill_score + advanced_bonus, 40),
            'max_score': 40,
            'display_value': f'{len(skills)} skill{"s" if len(skills) != 1 else ""}',
            'status': 'Strong' if len(skills) >= 3 else 'Needs attention',
            'explanation': 'This reflects how much relevant skill depth you have already declared.',
            'target': 'Reach at least 3 relevant skills, with one moving toward advanced.',
        },
        {
            'label': 'Roadmap execution',
            'score': roadmap_score,
            'max_score': 30,
            'display_value': f'{round((roadmap_completed / roadmap_total) * 100) if roadmap_total else 0}%',
            'status': 'Strong' if roadmap_total and roadmap_completed >= max(1, roadmap_total // 3) else 'Needs attention',
            'explanation': 'This measures how much of your planned path you have actually completed.',
            'target': 'Cross 30% roadmap completion to move from planning to evidence.',
        },
        {
            'label': 'Learning consistency',
            'score': min(consistency_score + streak_bonus, 30),
            'max_score': 30,
            'display_value': f'{streak_days} day{"s" if streak_days != 1 else ""}',
            'status': 'Strong' if streak_days >= 3 or weekly_goal['completed_this_week'] >= 2 else 'Needs attention',
            'explanation': 'This checks whether your progress is regular enough to compound.',
            'target': 'Build a 3-day streak and hit your weekly goal regularly.',
        },
    ]

    if readiness_label == 'Strong':
        summary = 'You are in a strong position. The main focus now is turning progress into interview-ready proof and sharper applications.'
    elif readiness_label == 'Building':
        summary = 'You have a workable base. Your biggest jump will come from closing your top gaps and being more consistent week to week.'
    else:
        summary = 'You are still setting up the foundation. A few focused improvements can move you forward quickly.'

    summary = {
        'score': readiness_score,
        'label': readiness_label,
        'summary': summary,
        'profile_completion': profile_completion,
        'skills_count': len(skills),
        'roadmap_completion': round((roadmap_completed / roadmap_total) * 100) if roadmap_total else 0,
        'consistency_score': min(weekly_goal['completion_percentage'], 100),
        'streak_days': streak_days,
        'components': components,
        'strengths': strengths[:4],
        'blockers': blockers[:4],
        'profile_checks': profile_checks,
        'next_label': next_label,
        'points_to_next_level': max(next_threshold - readiness_score, 0) if next_threshold else 0,
        'next_milestone': (
            f'You need {max(next_threshold - readiness_score, 0)} more point'
            f'{"s" if max(next_threshold - readiness_score, 0) != 1 else ""} to reach {next_label}.'
        ) if next_label and next_threshold else 'You are already in the top readiness band.',
    }
    cache.set(cache_key, summary, 60)
    return summary


SKILL_TRACK_BLUEPRINTS = {
    'development': {
        'label': 'Developer Track',
        'required_skills': ['Python', 'Django', 'SQL', 'Git', 'APIs', 'HTML', 'CSS', 'JavaScript'],
    },
    'analytical': {
        'label': 'Data / Analytics Track',
        'required_skills': ['SQL', 'Python', 'Pandas', 'Excel', 'Statistics', 'Power BI', 'Data Cleaning'],
    },
    'research': {
        'label': 'Research / AI Track',
        'required_skills': ['Python', 'Statistics', 'Machine Learning', 'NumPy', 'Pandas', 'Data Structures'],
    },
    'creative_ui': {
        'label': 'Frontend / UI Track',
        'required_skills': ['HTML', 'CSS', 'JavaScript', 'React', 'UI Design', 'Responsive Design', 'Git'],
    },
    'security': {
        'label': 'Security Track',
        'required_skills': ['Networking', 'Linux', 'Python', 'Security Basics', 'Web Security', 'Git'],
    },
}


def _normalize_skill_name(value):
    return ''.join(ch.lower() for ch in str(value or '') if ch.isalnum())


def _infer_skill_track(profile, domains):
    if profile.preferred_domain_work_type in SKILL_TRACK_BLUEPRINTS:
        return profile.preferred_domain_work_type

    domain_text = ' '.join(domains).lower()
    if any(keyword in domain_text for keyword in ['data', 'analytics', 'analysis']):
        return 'analytical'
    if any(keyword in domain_text for keyword in ['ai', 'ml', 'machine learning', 'research']):
        return 'research'
    if any(keyword in domain_text for keyword in ['design', 'frontend', 'ui']):
        return 'creative_ui'
    if any(keyword in domain_text for keyword in ['security', 'cyber']):
        return 'security'
    return 'development'


def _skill_gap_summary(user):
    cache_key = _user_cache_key('skill_gap_summary', user)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    profile = get_or_create_profile(user)
    domains = get_user_domains(profile)
    track_key = _infer_skill_track(profile, domains)
    track = SKILL_TRACK_BLUEPRINTS[track_key]

    current_skills = list(profile.skills.all())
    normalized_current_skills = {_normalize_skill_name(skill.skill_name): skill for skill in current_skills}

    strengths = []
    missing = []
    improving = []

    for required_skill in track['required_skills']:
        normalized_name = _normalize_skill_name(required_skill)
        matched_skill = normalized_current_skills.get(normalized_name)
        if matched_skill:
            if matched_skill.proficiency == 'advanced':
                strengths.append(required_skill)
            else:
                improving.append({
                    'name': required_skill,
                    'proficiency': matched_skill.get_proficiency_display(),
                })
        else:
            missing.append(required_skill)

    next_focus = missing[:3]
    if len(next_focus) < 3:
        next_focus.extend(item['name'] for item in improving[: 3 - len(next_focus)])

    coverage = round(((len(track['required_skills']) - len(missing)) / len(track['required_skills'])) * 100)
    focus_plan = []
    for skill in next_focus[:3]:
        if skill in missing:
            reason = 'Missing completely right now, so learning this will create the biggest jump in track fit.'
            action = f'Start with one beginner-friendly project or guided course for {skill}.'
        else:
            matched_item = next((item for item in improving if item['name'] == skill), None)
            proficiency = matched_item['proficiency'] if matched_item else 'Intermediate'
            reason = f'Already present at {proficiency.lower()} level, so deepening it is faster than starting from zero.'
            action = f'Practice {skill} through one applied task this week.'
        focus_plan.append({
            'skill': skill,
            'reason': reason,
            'action': action,
        })

    aligned_skills = [
        {'name': skill.skill_name, 'proficiency': skill.get_proficiency_display()}
        for skill in current_skills
        if _normalize_skill_name(skill.skill_name) in {_normalize_skill_name(required) for required in track['required_skills']}
    ]

    if coverage >= 75:
        simple_summary = 'Your skill profile matches this track well. Focus more on depth and proof-of-work now.'
    elif coverage >= 45:
        simple_summary = 'You have partial alignment. A few targeted skills can make this track much stronger.'
    else:
        simple_summary = 'You are still building the core skills for this track, so prioritization matters a lot.'

    summary = {
        'track_key': track_key,
        'track_label': track['label'],
        'coverage': coverage,
        'required_skills': track['required_skills'],
        'strengths': strengths[:5],
        'missing_skills': missing,
        'improving_skills': improving[:5],
        'next_focus': next_focus,
        'focus_plan': focus_plan,
        'aligned_skills': aligned_skills[:6],
        'matched_count': len(track['required_skills']) - len(missing),
        'missing_count': len(missing),
        'improving_count': len(improving),
        'simple_summary': simple_summary,
    }
    cache.set(cache_key, summary, 60)
    return summary


def _personalized_action_plan(user):
    cache_key = _user_cache_key('personalized_action_plan', user)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    roadmap_cards = _roadmap_dashboard_cards(user)
    weekly_goal = _weekly_goal_summary(user)
    readiness = _job_readiness_summary(user)
    skill_gap = _skill_gap_summary(user)

    primary_card = next((card for card in roadmap_cards if card['next_step']), roadmap_cards[0] if roadmap_cards else None)
    next_step = primary_card['next_step'] if primary_card else None
    steps_remaining_this_week = max(weekly_goal['remaining_steps'], 0)

    this_week = []
    blockers_to_clear = []

    if primary_card and next_step:
        this_week.append(
            f"Complete '{next_step.get('title', 'your next roadmap step')}' in {primary_card['snapshot'].title}."
        )
    elif not roadmap_cards:
        this_week.append("Generate and save one roadmap so your progress becomes trackable.")

    if skill_gap['next_focus']:
        this_week.append(f"Prioritize {', '.join(skill_gap['next_focus'][:2])} as your next core skill focus.")

    if steps_remaining_this_week > 0:
        this_week.append(
            f"Finish {steps_remaining_this_week} more roadmap step"
            f"{'s' if steps_remaining_this_week != 1 else ''} to hit this week's goal."
        )
    else:
        this_week.append("You've already hit this week's goal, so use the extra time for revision and mock practice.")

    if readiness['profile_completion'] < 80:
        blockers_to_clear.append("Fill in missing profile details to sharpen recommendations.")
    if skill_gap['missing_skills']:
        blockers_to_clear.append(f"Close the gap on {skill_gap['missing_skills'][0]} next.")
    if readiness['streak_days'] < 3:
        blockers_to_clear.append("Build a 3-day streak by finishing one roadmap step each day.")
    blockers_to_clear.extend(readiness['blockers'])

    deduped_blockers = []
    seen = set()
    for blocker in blockers_to_clear:
        key = blocker.strip().lower()
        if key and key not in seen:
            deduped_blockers.append(blocker)
            seen.add(key)

    today_focus = []
    if next_step:
        today_focus.append(next_step.get('title', 'Complete your next roadmap step'))
    if skill_gap['next_focus']:
        today_focus.append(f"Practice {skill_gap['next_focus'][0]}")
    if weekly_goal['completed_this_week'] == 0:
        today_focus.append("Log your first completed step this week")
    elif steps_remaining_this_week > 0:
        today_focus.append(f"Move weekly progress from {weekly_goal['completed_this_week']} to {weekly_goal['goal'].target_steps}")
    else:
        today_focus.append("Review completed steps and prepare the next milestone")

    headline = (
        "Push execution this week"
        if readiness['score'] >= 55
        else "Build your learning foundation this week"
    )

    this_month = []
    if readiness['profile_completion'] < 80:
        this_month.append('Finish the missing profile details so future recommendations become more accurate.')
    if skill_gap['next_focus']:
        this_month.append(f"Move at least one of these skills into a stronger level: {', '.join(skill_gap['next_focus'][:2])}.")
    if roadmap_cards:
        this_month.append('Take one saved roadmap to at least one-third completion.')
    else:
        this_month.append('Generate, save, and begin your first roadmap.')

    quick_wins = []
    if next_step:
        quick_wins.append(f"Complete the next roadmap step: {next_step.get('title', 'your next roadmap step')}.")
    if skill_gap['next_focus']:
        quick_wins.append(f"Spend 30 focused minutes on {skill_gap['next_focus'][0]}.")
    if weekly_goal['completed_this_week'] == 0:
        quick_wins.append('Log one completed learning step today to start momentum.')
    else:
        quick_wins.append('Protect your streak by completing one more meaningful task today.')

    summary = {
        'headline': headline,
        'summary': (
            f"You are in the {readiness['label'].lower()} stage for the {skill_gap['track_label'].lower()}."
            f" Focus on the highest-ROI next step instead of spreading effort too wide."
        ),
        'coach_note': (
            'The goal is not to do everything. The goal is to make your next move obvious and small enough to finish.'
        ),
        'this_week': this_week[:4],
        'today_focus': today_focus[:3],
        'this_month': this_month[:3],
        'quick_wins': quick_wins[:3],
        'blockers_to_clear': deduped_blockers[:4],
        'primary_roadmap_title': primary_card['snapshot'].title if primary_card else '',
        'recommended_next_step': next_step.get('title', '') if next_step else '',
    }
    cache.set(cache_key, summary, 60)
    return summary


PROJECT_GUIDANCE_BLUEPRINTS = {
    'development': {
        'portfolio_title': 'Backend Portfolio Builder',
        'projects': [
            {
                'title': 'Job Board API',
                'level': 'Beginner to Intermediate',
                'outcome': 'Build authentication, CRUD, filtering, and deployment-ready APIs.',
                'skills': ['Django', 'REST APIs', 'SQL'],
            },
            {
                'title': 'Issue Tracker Platform',
                'level': 'Intermediate',
                'outcome': 'Show database design, workflows, roles, and reporting.',
                'skills': ['Python', 'Django', 'PostgreSQL', 'UI flows'],
            },
        ],
        'proof_points': ['Authentication', 'API design', 'Database modeling', 'Deployment'],
    },
    'analytical': {
        'portfolio_title': 'Data Analyst Proof-of-Work',
        'projects': [
            {
                'title': 'Product Metrics Dashboard',
                'level': 'Beginner to Intermediate',
                'outcome': 'Analyze funnels, retention, and growth metrics on a realistic dataset.',
                'skills': ['SQL', 'Pandas', 'Power BI'],
            },
            {
                'title': 'A/B Testing Case Study',
                'level': 'Intermediate',
                'outcome': 'Present a business recommendation backed by data cleaning and hypothesis testing.',
                'skills': ['Statistics', 'Excel', 'Python'],
            },
        ],
        'proof_points': ['SQL depth', 'Real datasets', 'Business storytelling', 'Dashboards'],
    },
    'research': {
        'portfolio_title': 'AI / Research Portfolio',
        'projects': [
            {
                'title': 'Model Comparison Notebook',
                'level': 'Beginner to Intermediate',
                'outcome': 'Compare baseline models and explain tradeoffs with clear metrics.',
                'skills': ['Python', 'Pandas', 'Machine Learning'],
            },
            {
                'title': 'Domain-Specific Predictor',
                'level': 'Intermediate',
                'outcome': 'Train and evaluate a practical model on a real-world dataset.',
                'skills': ['NumPy', 'Feature Engineering', 'Evaluation'],
            },
        ],
        'proof_points': ['Model evaluation', 'Experiment tracking', 'Clear problem framing', 'Result explanation'],
    },
    'creative_ui': {
        'portfolio_title': 'Frontend Showcase',
        'projects': [
            {
                'title': 'Responsive SaaS Landing Experience',
                'level': 'Beginner to Intermediate',
                'outcome': 'Demonstrate polished layout, animation, accessibility, and responsive behavior.',
                'skills': ['HTML', 'CSS', 'Responsive Design'],
            },
            {
                'title': 'Interactive Dashboard UI',
                'level': 'Intermediate',
                'outcome': 'Show reusable components, state-driven rendering, and clean information hierarchy.',
                'skills': ['JavaScript', 'React', 'UI Design'],
            },
        ],
        'proof_points': ['Responsive UI', 'Component thinking', 'Accessibility', 'Visual polish'],
    },
    'security': {
        'portfolio_title': 'Security Practice Portfolio',
        'projects': [
            {
                'title': 'Web Security Audit Lab',
                'level': 'Beginner to Intermediate',
                'outcome': 'Document common vulnerabilities, fixes, and verification steps.',
                'skills': ['Web Security', 'Python', 'OWASP basics'],
            },
            {
                'title': 'Network Hardening Checklist',
                'level': 'Intermediate',
                'outcome': 'Show process thinking through hardening, monitoring, and incident notes.',
                'skills': ['Networking', 'Linux', 'Security Basics'],
            },
        ],
        'proof_points': ['Threat awareness', 'Security documentation', 'Practical fixes', 'System thinking'],
    },
}


def _project_guidance_summary(user):
    cache_key = _user_cache_key('project_guidance_summary', user)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    profile = get_or_create_profile(user)
    domains = get_user_domains(profile)
    track_key = _infer_skill_track(profile, domains)
    track = PROJECT_GUIDANCE_BLUEPRINTS[track_key]
    skill_gap = _skill_gap_summary(user)
    readiness = _job_readiness_summary(user)

    missing = skill_gap['missing_skills']
    starter_project = track['projects'][0]
    stretch_project = track['projects'][1]

    build_order = [starter_project['title']]
    if readiness['score'] >= 45:
        build_order.append(stretch_project['title'])

    project_focus = missing[:2] if missing else starter_project['skills'][:2]

    summary = {
        'track_label': track['portfolio_title'],
        'starter_project': starter_project,
        'stretch_project': stretch_project,
        'build_order': build_order,
        'project_focus': project_focus,
        'proof_points': track['proof_points'],
        'summary': (
            f"Build projects that prove you can execute in the {skill_gap['track_label'].lower()}."
            f" Focus on outcomes that are visible in interviews and on a portfolio."
        ),
    }
    cache.set(cache_key, summary, 60)
    return summary


def _application_readiness_summary(user):
    cache_key = _user_cache_key('application_readiness_summary', user)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    readiness = _job_readiness_summary(user)
    skill_gap = _skill_gap_summary(user)
    action_plan = _personalized_action_plan(user)
    project_guidance = _project_guidance_summary(user)

    checks = [
        {
            'label': 'Profile foundation',
            'status': readiness['profile_completion'] >= 80,
            'detail': f"{readiness['profile_completion']}% complete",
        },
        {
            'label': 'Skill coverage',
            'status': skill_gap['coverage'] >= 60,
            'detail': f"{skill_gap['coverage']}% aligned to {skill_gap['track_label']}",
        },
        {
            'label': 'Portfolio proof',
            'status': readiness['score'] >= 45,
            'detail': project_guidance['starter_project']['title'],
        },
        {
            'label': 'Execution consistency',
            'status': readiness['streak_days'] >= 3,
            'detail': f"{readiness['streak_days']} day streak",
        },
        {
            'label': 'Roadmap execution',
            'status': readiness['roadmap_completion'] >= 25,
            'detail': f"{readiness['roadmap_completion']}% roadmap completion",
        },
    ]

    passed_checks = sum(1 for check in checks if check['status'])
    score = round((passed_checks / len(checks)) * 100)

    if score >= 80:
        status_label = 'Ready To Apply'
    elif score >= 50:
        status_label = 'Almost Ready'
    else:
        status_label = 'Needs More Prep'

    next_gaps = [check for check in checks if not check['status']]
    next_actions = []
    if next_gaps:
        next_actions.append(f"Fix {next_gaps[0]['label'].lower()} first.")
    next_actions.extend(action_plan['this_week'][:2])

    ready_count = passed_checks
    total_checks = len(checks)
    if score >= 80:
        plain_english = 'You can start applying, but keep improving proof-of-work and interview readiness alongside applications.'
    elif score >= 50:
        plain_english = 'You are close, but a few missing pieces are still lowering confidence and conversion.'
    else:
        plain_english = 'It is better to strengthen your base first so your applications have a stronger chance of working.'

    summary = {
        'score': score,
        'status_label': status_label,
        'checks': checks,
        'ready_count': ready_count,
        'total_checks': total_checks,
        'next_actions': next_actions[:3],
        'summary': (
            f"This checklist estimates whether you can start applying confidently for opportunities in the "
            f"{skill_gap['track_label'].lower()}."
        ),
        'plain_english': plain_english,
    }
    cache.set(cache_key, summary, 60)
    return summary


OPPORTUNITY_BLUEPRINTS = {
    'development': {
        'roles': [
            ('Backend Developer Intern', 'Product Startup'),
            ('Junior Django Developer', 'SaaS Company'),
            ('API Engineer Intern', 'Fintech Team'),
        ],
        'priority_companies': ['Amazon', 'Atlassian', 'Razorpay'],
        'timeline_focus': ['DSA basics', 'Django APIs', 'SQL queries', 'Projects and resume'],
    },
    'analytical': {
        'roles': [
            ('Data Analyst Intern', 'Growth Team'),
            ('Business Analyst Intern', 'E-commerce Company'),
            ('Product Analyst Associate', 'Consumer App'),
        ],
        'priority_companies': ['Meta', 'Google', 'TCS'],
        'timeline_focus': ['SQL depth', 'Case studies', 'Dashboards', 'Statistics revision'],
    },
    'research': {
        'roles': [
            ('ML Intern', 'AI Startup'),
            ('Research Assistant', 'Applied AI Lab'),
            ('Junior Data Scientist', 'Product Analytics Team'),
        ],
        'priority_companies': ['Google', 'NVIDIA', 'Adobe'],
        'timeline_focus': ['Python', 'Model evaluation', 'Experiments', 'Applied projects'],
    },
    'creative_ui': {
        'roles': [
            ('Frontend Developer Intern', 'SaaS Company'),
            ('UI Engineer Intern', 'Design-first Startup'),
            ('Junior React Developer', 'Product Team'),
        ],
        'priority_companies': ['Meta', 'Swiggy', 'Zomato'],
        'timeline_focus': ['Responsive UI', 'JavaScript depth', 'React projects', 'Portfolio polish'],
    },
    'security': {
        'roles': [
            ('Security Analyst Intern', 'Enterprise Team'),
            ('SOC Intern', 'Security Operations'),
            ('Application Security Trainee', 'Product Company'),
        ],
        'priority_companies': ['Infosys', 'Wipro', 'Microsoft'],
        'timeline_focus': ['Networking', 'Linux', 'OWASP basics', 'Security documentation'],
    },
}


def _opportunity_layer_summary(user):
    cache_key = _user_cache_key('opportunity_layer_summary', user)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    profile = get_or_create_profile(user)
    domains = get_user_domains(profile)
    track_key = _infer_skill_track(profile, domains)
    blueprint = OPPORTUNITY_BLUEPRINTS[track_key]
    readiness = _application_readiness_summary(user)
    skill_gap = _skill_gap_summary(user)
    project_guidance = _project_guidance_summary(user)

    matched_roles = []
    for index, (title, company) in enumerate(blueprint['roles'], start=1):
        matched_roles.append({
            'title': title,
            'company': company,
            'mode': profile.get_preferred_work_mode_display(),
            'fit_reason': (
                f"Matches your {skill_gap['track_label'].lower()} focus and current"
                f" strength around {', '.join(skill_gap['next_focus'][:2] or project_guidance['project_focus'][:2])}."
            ),
            'readiness_note': 'Apply now' if readiness['score'] >= 80 or index == 1 else 'Prepare for next cycle',
        })

    month_names = []
    today = timezone.localdate()
    for offset in range(4):
        month_date = (today.replace(day=1) + timedelta(days=32 * offset)).replace(day=1)
        month_names.append(month_date.strftime('%B'))

    hiring_calendar = [
        {
            'month': month_names[0],
            'focus': 'Resume refresh and shortlist building',
            'window': 'Early-month applications and outreach',
        },
        {
            'month': month_names[1],
            'focus': 'Assessments and first interview rounds',
            'window': 'Practice tests and company-specific prep',
        },
        {
            'month': month_names[2],
            'focus': 'Project proof and mock interviews',
            'window': 'Strengthen portfolio and communication',
        },
        {
            'month': month_names[3],
            'focus': 'Conversion push',
            'window': 'Apply with refined profile and follow-ups',
        },
    ]

    company_timelines = []
    for company in blueprint['priority_companies']:
        company_timelines.append({
            'company': company,
            'weeks': [
                f"Week 1: Prioritize {blueprint['timeline_focus'][0]}",
                f"Week 2: Drill {blueprint['timeline_focus'][1]}",
                f"Week 3: Build proof around {blueprint['timeline_focus'][2]}",
                f"Week 4: Final review with {blueprint['timeline_focus'][3]}",
            ],
        })

    alert_stream = [
        {
            'title': f"New {matched_roles[0]['title']} style openings",
            'detail': f"Watch for {profile.get_preferred_work_mode_display().lower()} roles aligned to {skill_gap['track_label']}.",
        },
        {
            'title': 'Shortlist companies to track',
            'detail': f"Focus alerts on {', '.join(blueprint['priority_companies'][:2])} first for higher fit opportunities.",
        },
        {
            'title': 'Resume trigger',
            'detail': f"Refresh your resume after finishing {project_guidance['starter_project']['title']} to improve conversion.",
        },
    ]

    summary = {
        'headline': f"Opportunity Hub for {skill_gap['track_label']}",
        'matched_roles': matched_roles,
        'hiring_calendar': hiring_calendar,
        'company_timelines': company_timelines,
        'alerts': alert_stream,
        'readiness_label': readiness['status_label'],
    }
    cache.set(cache_key, summary, 60)
    return summary


def _serialize_course(course, saved_lookup=None):
    payload = {
        'title': course.title,
        'platform': course.platform,
        'level': course.level,
        'level_display': course.get_level_display(),
        'domain': course.domain,
        'is_free': course.is_free,
        'learning_type': course.learning_type,
        'learning_type_display': course.get_learning_type_display(),
        'duration': course.duration,
        'link': course.link,
        'description': course.description,
        'is_ai': course.is_ai_generated,
        'is_external': course.is_external,
    }
    payload.update(_saved_payload_for_object(course, saved_lookup or set()))
    return payload


def _serialize_book(book, saved_lookup=None):
    payload = {
        'title': book.title,
        'author': book.author,
        'domain': book.domain,
        'level': book.level,
        'book_type': book.book_type,
        'description': book.description,
        'link': book.link,
        'is_ai_generated': book.is_ai_generated,
    }
    payload.update(_saved_payload_for_object(book, saved_lookup or set()))
    return payload


def _serialize_tool(tool, saved_lookup=None):
    payload = {
        'name': tool.name,
        'category': tool.category,
        'description': tool.description,
        'use_case': tool.use_case,
        'difficulty': tool.difficulty,
        'link': tool.link,
        'is_ai_generated': tool.is_ai_generated,
    }
    payload.update(_saved_payload_for_object(tool, saved_lookup or set()))
    return payload


def _serialize_tip(tip, saved_lookup=None):
    payload = {
        'title': tip.title,
        'explanation': tip.explanation,
        'action_step': tip.action_step,
        'category': tip.category,
        'icon': tip.icon,
        'is_ai_generated': tip.is_ai_generated,
    }
    payload.update(_saved_payload_for_object(tip, saved_lookup or set()))
    return payload


def _is_object_saved_for_user(user, model_class, object_id):
    if not user.is_authenticated:
        return False
    return SavedItem.objects.filter(
        user=user,
        content_type=ContentType.objects.get_for_model(model_class),
        object_id=object_id,
    ).exists()


def _render_resource_detail(request, resource, resource_type, title, description, link, meta_items):
    return render(request, 'resource_detail.html', {
        'resource': resource,
        'resource_type': resource_type,
        'resource_title': title,
        'resource_description': description,
        'resource_link': link,
        'is_saved': _is_object_saved_for_user(request.user, resource.__class__, resource.pk),
        'meta_items': meta_items,
    })


def _redirect_to_progress_section(section_id):
    return redirect(f"{reverse('techbrat:progress_dashboard')}#{section_id}")

def index(request):
    # If user is not authenticated, show landing page
    if not request.user.is_authenticated:
        return render(request, 'landing_auth.html', get_landing_context(request))
    return render(request, 'index.html')

@login_required(login_url='techbrat:signin')
def issue(request):
    return render(request, 'issue.html')

@login_required(login_url='techbrat:signin')
def que(request):
    return render(request, 'que.html')

def roadmap(request):
    return render(request, 'roadmap.html')


def _build_snapshot_fingerprint(payload):
    normalized = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def _clean_text(value):
    if not value:
        return ''
    return ' '.join(str(value).replace('\r', '\n').split())


COMPANY_PREP_PROMPT = """
You are a senior Data Analyst hiring mentor with deep knowledge of REAL hiring patterns at top companies like Meta, Amazon, Google, and TCS.

Your task is to generate a HIGHLY DETAILED, PRACTICAL, and COMPANY-SPECIFIC preparation plan.

DO NOT give generic advice.

---

USER PROFILE:

Company: {company}
Role: {role}
Experience Level: {experience_level}
Skills: {skills}
Strong Areas: {strong_areas}
Weak Areas: {weak_areas}
Time Available: {hours_per_day} hours/day for {total_time}
Current Status: {current_status}
Target Package: {target_package}

---

STRICT INSTRUCTIONS:

1. Give COMPANY-SPECIFIC insights (Meta != generic company).
2. Mention EXACT SKILLS required for THIS role.
3. Break down topics into SUBTOPICS (not broad terms like "DSA").
4. Give REALISTIC roadmap based on time.
5. Include SPECIFIC practice tasks.
6. Avoid vague words like "improve problem solving".
7. If beginner, start from basics but move fast.
8. Prioritize HIGH ROI topics only.
9. If the company is product-based (Meta, Amazon, Google), increase difficulty and emphasize SQL, case studies, and real-world datasets.
10. If the company is service-based (TCS, Infosys), emphasize aptitude and basic coding.
""".strip()


PRODUCT_BASED_COMPANIES = {"meta", "amazon", "google", "microsoft", "netflix", "adobe", "uber", "atlassian"}
SERVICE_BASED_COMPANIES = {"tcs", "infosys", "wipro", "cognizant", "capgemini", "accenture"}


def _company_bucket(company_name):
    normalized = (company_name or "").strip().lower()
    if normalized in PRODUCT_BASED_COMPANIES:
        return "product"
    if normalized in SERVICE_BASED_COMPANIES:
        return "service"
    return "general"


def _company_prep_blueprint(company, role, prep_inputs):
    bucket = _company_bucket(company)
    role_lower = role.lower()
    weak_areas = prep_inputs["weak_areas"]
    hours = prep_inputs["hours_per_day"]
    total_time = prep_inputs["total_time"]

    if "data analyst" in role_lower:
        exact_skills = [
            "SQL: INNER JOIN, LEFT JOIN, GROUP BY, HAVING, subqueries, CTEs, window functions",
            "Python: Pandas DataFrames, data cleaning, missing values, exploratory analysis",
            "Statistics: probability, distributions, hypothesis testing, A/B testing basics",
            "Product Sense: metrics, funnels, retention, segmentation, user behavior analysis",
        ]
        topic_breakdown = [
            "SQL: INNER JOIN, LEFT JOIN, GROUP BY, HAVING, ROW_NUMBER, RANK, moving aggregates",
            "Python: DataFrames, filtering, merging, missing values, feature summaries",
            "Statistics: probability basics, confidence intervals, hypothesis testing, A/B testing intuition",
            "Business Analysis: KPI selection, funnel drop-offs, dashboard storytelling",
        ]
        practice_targets = [
            "SQL: 100 role-focused problems with joins, window functions, and metrics questions",
            "Python: 5 mini analysis projects using real datasets",
            "Case studies: 10 product or business analysis cases",
        ]
    elif "backend" in role_lower:
        exact_skills = [
            "Data structures: arrays, hash maps, stacks, queues, trees, graphs",
            "Backend fundamentals: APIs, authentication, database design, caching, background jobs",
            "SQL: joins, indexing basics, aggregation, query optimization",
            "System basics: scalability, rate limiting, logging, monitoring",
        ]
        topic_breakdown = [
            "DSA: arrays, strings, hash maps, sliding window, binary search, trees, BFS/DFS",
            "Backend: REST APIs, auth flows, DB schema design, pagination, caching",
            "SQL: joins, indexes, aggregation, EXPLAIN basics, query tradeoffs",
            "Behavioral: project storytelling, debugging decisions, ownership examples",
        ]
        practice_targets = [
            "DSA: 60-80 focused problems with timed practice",
            "Backend: 2 mock API builds and 1 production-style project review",
            "System design: 8 lightweight design prompts for your level",
        ]
    else:
        exact_skills = [
            "Core role concepts aligned to the target position",
            "Problem solving and interview communication",
            "SQL or coding basics relevant to the role",
            "Resume/project storytelling with measurable impact",
        ]
        topic_breakdown = [
            "Core concepts required for the role",
            "Common interview exercises and implementation tasks",
            "Weak-area revision with measurable practice goals",
            "Behavioral preparation and role-fit communication",
        ]
        practice_targets = [
            "Core interview practice: 40-60 focused questions",
            "Role-specific mini tasks or case drills",
            "Mock interviews: at least 5 structured sessions",
        ]

    if bucket == "product":
        hiring_process = [
            f"Resume Screening: {company} screens for impact, ownership, and role-fit evidence.",
            "Technical / Assessment Round: expect deeper SQL, coding, or analytical exercises with less hand-holding.",
            "Case / Product Round: expect product thinking, metrics discussion, tradeoffs, and real-world reasoning.",
            "Behavioral Round: strong emphasis on communication, ownership, prioritization, and structured thinking.",
        ]
        focus_areas = [
            f"Prioritize high-difficulty topics for {role}, especially SQL depth, case studies, and realistic datasets.",
            f"Use {weak_areas} as the first daily revision block until they stop being interview risks.",
            "Practice explaining tradeoffs and business impact, not just final answers.",
        ]
        difficulty = f"{company} is product-based, so the bar is higher on depth, speed, and real-world judgment."
        company_tips = [
            f"Focus heavily on SQL depth and case-style reasoning for {company}.",
            "Practice with realistic datasets instead of only toy examples.",
            "Be ready to explain thought process, assumptions, and tradeoffs clearly.",
        ]
    elif bucket == "service":
        hiring_process = [
            f"Resume Screening: {company} checks academic basics, consistency, and trainability.",
            "Aptitude / Assessment Round: expect quant, logical reasoning, verbal ability, and basic coding.",
            "Technical Round: expect fundamentals, simple coding or SQL, and clear explanation of basics.",
            "HR / Fit Round: expect communication, flexibility, learning attitude, and project discussion.",
        ]
        focus_areas = [
            "Prioritize aptitude, structured basics, and reliable interview fundamentals.",
            f"Use {weak_areas} to build a short daily drill routine with measurable repetition.",
            "Revise basic coding, problem solving structure, and communication discipline.",
        ]
        difficulty = f"{company} is service-based, so clearing aptitude and fundamentals reliably matters more than advanced specialization."
        company_tips = [
            f"Do not ignore aptitude for {company}; it is often an elimination round.",
            "Be very solid on core basics and simple coding problems.",
            "Practice concise communication and confidence in HR-style answers.",
        ]
        if "Data structures" not in exact_skills[0]:
            exact_skills.append("Aptitude: quant, logical reasoning, verbal basics")
        topic_breakdown.append("Aptitude: percentages, ratios, time and work, logical sets, verbal basics")
        practice_targets.append("Aptitude: 15 timed sets with accuracy tracking")
    else:
        hiring_process = [
            f"Resume Screening: {company} will look for role-fit and preparation consistency.",
            "Assessment / Technical Round: expect role-relevant fundamentals and applied problem solving.",
            "Role Round: expect project discussion, practical judgment, and communication.",
            "HR / Final Round: expect motivation, compensation discussion, and team fit.",
        ]
        focus_areas = [
            f"Center the plan on {role}-specific fundamentals and the biggest gaps in {weak_areas}.",
            "Keep practice measurable so progress is visible each week.",
            "Balance technical revision with mock interview communication.",
        ]
        difficulty = "The difficulty depends on how well your current skills and weak areas align with the target role."
        company_tips = [
            f"Study the job description for {company} closely and mirror your prep to it.",
            "Use mock interviews to find gaps before the final week.",
            "Keep revision focused on high-frequency topics, not everything at once.",
        ]

    roadmap = [
        f"Week 1: Audit your current status ({prep_inputs['current_status']}) and lock in the highest-ROI topics for {role}.",
        f"Week 2: Spend {hours} hour(s) daily on weak-area repair, targeted practice tasks, and one role-specific build or case track.",
        f"Week 3+: Shift into timed drills, realistic mock rounds, and company-specific revision for the remaining {total_time}.",
    ]

    if "1 month" in total_time.lower() or "4 week" in total_time.lower():
        roadmap = [
            f"Week 1: Cover foundations fast and clean up the biggest blockers in {weak_areas}.",
            "Week 2: Push heavy practice on exact interview topics and start timed problem sets.",
            "Week 3: Add mock interviews, company-specific scenarios, and revision of repeated mistakes.",
            "Week 4: Focus on final revision, mock rounds, and verbal explanation quality.",
        ]

    daily_plan = [
        f"{max(1, hours // 2)} hour: highest-priority weak-area practice",
        f"{max(1, hours - max(1, hours // 2))} hour: role-specific questions, cases, or project tasks",
        "15-20 mins: revision notes, mistakes log, and next-day planning",
    ]

    resources = [
        "Official documentation for the role stack",
        "Company interview experiences and question banks",
        "Mock interviews and structured revision notes",
    ]
    if "data analyst" in role_lower:
        resources = [
            "SQL: LeetCode SQL section",
            "Python: Kaggle datasets for practice",
            "Statistics: Khan Academy for A/B testing basics",
        ]

    common_mistakes = [
        f"Ignoring {weak_areas} until the last week",
        "Studying broad topics without role-specific practice tasks",
        "Skipping mock interviews or verbal explanation practice",
    ]

    final_strategy = [
        "Last 5 days: only revision, mocks, and mistake correction",
        "Review notes daily instead of reopening every topic from scratch",
        "Practice explaining solutions verbally in interview style",
    ]

    return {
        "difficulty": difficulty,
        "hiring_process": hiring_process,
        "exact_skills": exact_skills,
        "topic_breakdown": topic_breakdown,
        "focus_areas": focus_areas,
        "roadmap": roadmap,
        "daily_plan": daily_plan,
        "practice_targets": practice_targets,
        "resources": resources,
        "common_mistakes": common_mistakes,
        "company_tips": company_tips,
        "final_strategy": final_strategy,
    }


def generate_ai_response(prompt):
    return "Sample response"


def _format_company_prep_output(company, role, prep_inputs, plan):
    target_package = prep_inputs.get('target_package') or 'Not specified'
    lines = [
        "Target",
        f"- {company} - {role}",
        f"- Experience Level: {prep_inputs['experience_level']}",
        f"- Time Available: {prep_inputs['hours_per_day']} hour(s)/day for {prep_inputs['total_time']}",
        f"- Current Status: {prep_inputs['current_status']}",
        f"- Target Package: {target_package}",
        "",
        "Difficulty Level",
        f"- {plan['difficulty']}",
        "",
        f"Actual Hiring Process at {company} ({role})",
    ]
    lines.extend(f"- {item}" for item in plan["hiring_process"])
    lines.extend([
        "",
        "Exact Skills Required",
    ])
    lines.extend(f"- {item}" for item in plan["exact_skills"])
    lines.extend([
        "",
        "Topic Breakdown",
    ])
    lines.extend(f"- {item}" for item in plan["topic_breakdown"])
    lines.extend([
        "",
        "Focus Areas",
    ])
    lines.extend(f"- {item}" for item in plan["focus_areas"])
    lines.extend([
        "",
        f"Personalized Roadmap ({prep_inputs['total_time']})",
    ])
    lines.extend(f"- {item}" for item in plan["roadmap"])
    lines.extend([
        "",
        f"Daily Plan ({prep_inputs['hours_per_day']} hours/day)",
    ])
    lines.extend(f"- {item}" for item in plan["daily_plan"])
    lines.extend([
        "",
        "Practice Targets",
    ])
    lines.extend(f"- {item}" for item in plan["practice_targets"])
    lines.extend([
        "",
        "Best Resources",
    ])
    lines.extend(f"- {item}" for item in plan["resources"])
    lines.extend([
        "",
        "Common Mistakes",
    ])
    lines.extend(f"- {item}" for item in plan["common_mistakes"])
    lines.extend([
        "",
        f"{company}-Specific Tips",
    ])
    lines.extend(f"- {item}" for item in plan["company_tips"])
    lines.extend([
        "",
        "Final Strategy",
    ])
    lines.extend(f"- {item}" for item in plan["final_strategy"])
    return "\n".join(lines)


@login_required(login_url='techbrat:signin')
def generate_company_prep(request):
    if request.method == 'GET':
        return render(request, 'company_prep.html')

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only GET and POST requests are allowed.'}, status=405)

    try:
        if request.content_type and 'application/json' in request.content_type:
            body = json.loads(request.body.decode('utf-8'))
        else:
            body = request.POST
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid request payload.'}, status=400)

    company = str(body.get('company', '')).strip()
    role = str(body.get('role', '')).strip()

    if not company or not role:
        return JsonResponse({'success': False, 'error': 'Company and role are required.'}, status=400)

    try:
        hours_per_day = int(body.get('hours_per_day', 0))
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Hours per day must be a valid number.'}, status=400)

    if hours_per_day < 1:
        return JsonResponse({'success': False, 'error': 'Hours per day must be at least 1.'}, status=400)

    prep_inputs = {
        'experience_level': str(body.get('experience_level') or 'Beginner').strip() or 'Beginner',
        'skills': str(body.get('skills') or 'No skills shared').strip() or 'No skills shared',
        'strong_areas': str(body.get('strong_areas') or 'Not specified').strip() or 'Not specified',
        'weak_areas': str(body.get('weak_areas') or 'Not specified').strip() or 'Not specified',
        'hours_per_day': hours_per_day,
        'total_time': str(body.get('total_time') or '4 weeks').strip() or '4 weeks',
        'current_status': str(body.get('current_status') or 'Getting started').strip() or 'Getting started',
        'target_package': str(body.get('target_package') or '').strip(),
    }

    prompt = COMPANY_PREP_PROMPT.format(
        company=company,
        role=role,
        experience_level=prep_inputs['experience_level'],
        skills=prep_inputs['skills'],
        strong_areas=prep_inputs['strong_areas'],
        weak_areas=prep_inputs['weak_areas'],
        hours_per_day=prep_inputs['hours_per_day'],
        total_time=prep_inputs['total_time'],
        current_status=prep_inputs['current_status'],
        target_package=prep_inputs['target_package'] or 'Not specified',
    )

    try:
        _ = generate_ai_response(prompt)
        plan = _company_prep_blueprint(company, role, prep_inputs)
        formatted_output = _format_company_prep_output(company, role, prep_inputs, plan)
        return JsonResponse({'success': True, 'data': formatted_output})
    except Exception:
        return JsonResponse({'success': False, 'error': 'Could not generate the preparation plan.'}, status=500)


@require_POST
def toggle_save(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Authentication required'}, status=401)

    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)

    object_id = body.get('object_id')
    model_type = body.get('content_type')
    model_class = _get_saveable_model(model_type)

    if not model_class or not object_id:
        return JsonResponse({'error': 'Invalid save target'}, status=400)

    target_object = get_object_or_404(model_class, pk=object_id)
    content_type = ContentType.objects.get_for_model(model_class)

    saved_item, created = SavedItem.objects.get_or_create(
        user=request.user,
        content_type=content_type,
        object_id=target_object.pk,
    )

    if not created:
        saved_item.delete()

    return JsonResponse({
        'saved': created,
        'content_type': content_type.model,
        'object_id': target_object.pk,
        'saved_count': SavedItem.objects.filter(user=request.user).count(),
    })


@login_required(login_url='techbrat:signin')
def saved_items(request):
    items = (
        SavedItem.objects.filter(user=request.user)
        .select_related('content_type')
        .order_by('-created_at')
    )
    roadmap_cards = _roadmap_dashboard_cards(request.user)
    total_roadmap_steps = sum(card['total_steps'] for card in roadmap_cards)
    completed_roadmap_steps = sum(card['completed_steps'] for card in roadmap_cards)
    return render(request, 'saved_items.html', {
        'saved_items': items,
        'roadmap_snapshot_count': len(roadmap_cards),
        'completed_roadmap_steps': completed_roadmap_steps,
        'total_roadmap_steps': total_roadmap_steps,
    })


@login_required(login_url='techbrat:signin')
def progress_dashboard(request):
    roadmap_cards = _roadmap_dashboard_cards(request.user)
    total_saved_roadmaps = len(roadmap_cards)
    total_steps = sum(card['total_steps'] for card in roadmap_cards)
    completed_steps = sum(card['completed_steps'] for card in roadmap_cards)
    overall_completion = round((completed_steps / total_steps) * 100) if total_steps else 0
    weekly_goal = _weekly_goal_summary(request.user)
    streak_days = _current_streak_days(request.user)
    readiness = _job_readiness_summary(request.user)
    skill_gap = _skill_gap_summary(request.user)
    action_plan = _personalized_action_plan(request.user)
    project_guidance = _project_guidance_summary(request.user)
    application_readiness = _application_readiness_summary(request.user)
    progress_overview = _progress_overview_summary(
        request.user,
        roadmap_cards,
        weekly_goal,
        streak_days,
        readiness,
        skill_gap,
    )

    return render(request, 'progress_dashboard.html', {
        'roadmap_cards': roadmap_cards,
        'total_saved_roadmaps': total_saved_roadmaps,
        'completed_steps': completed_steps,
        'total_steps': total_steps,
        'overall_completion': overall_completion,
        'weekly_goal': weekly_goal,
        'streak_days': streak_days,
        'readiness': readiness,
        'skill_gap': skill_gap,
        'action_plan': action_plan,
        'project_guidance': project_guidance,
        'application_readiness': application_readiness,
        'progress_overview': progress_overview,
    })


@login_required(login_url='techbrat:signin')
def job_readiness_page(request):
    return _redirect_to_progress_section('job-readiness')


@login_required(login_url='techbrat:signin')
def skill_gap_page(request):
    return _redirect_to_progress_section('skill-gap')


@login_required(login_url='techbrat:signin')
def action_plan_page(request):
    return _redirect_to_progress_section('action-plan')


@login_required(login_url='techbrat:signin')
def portfolio_guidance_page(request):
    return _redirect_to_progress_section('portfolio-guidance')


@login_required(login_url='techbrat:signin')
def application_readiness_page(request):
    return _redirect_to_progress_section('application-readiness')


@login_required(login_url='techbrat:signin')
def opportunity_hub_page(request):
    profile = get_or_create_profile(request.user)
    domains = get_user_domains(profile)
    readiness = _application_readiness_summary(request.user)
    skill_gap = _skill_gap_summary(request.user)

    opportunity_hub = {
        'headline': 'Live internships and jobs matched to what you want to build next',
        'readiness_label': readiness.get('status_label', 'Needs More Prep'),
        'default_keyword': '',
        'track_label': skill_gap.get('track_label', 'your target track'),
        'source_options': [
            {'value': key, 'label': label}
            for key, label in SOURCE_LABELS.items()
        ],
    }
    return render(request, 'opportunity_hub_live.html', {
        'opportunity_hub': opportunity_hub,
    })


def _query_flag(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


@login_required(login_url='techbrat:signin')
@require_GET
def live_opportunities(request):
    source = (request.GET.get('source') or 'all').strip().lower()
    opportunity_type = (request.GET.get('opportunity_type') or 'all').strip().lower()
    try:
        page = max(int(request.GET.get('page', 1)), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        limit = min(max(int(request.GET.get('limit', 12)), 1), 30)
    except (TypeError, ValueError):
        limit = 12
    filters = {
        'keyword': (request.GET.get('keyword') or '').strip(),
        'location': (request.GET.get('location') or '').strip(),
        'source': source if source in {'all', *SOURCE_LABELS.keys()} else 'all',
        'opportunity_type': opportunity_type if opportunity_type in {'all', 'internship', 'job'} else 'all',
        'region': (request.GET.get('region') or 'all').strip().lower() if (request.GET.get('region') or 'all').strip().lower() in {'all', 'india', 'abroad'} else 'all',
        'remote_only': _query_flag(request.GET.get('remote_only')),
        'paid_only': _query_flag(request.GET.get('paid_only')),
    }

    cache_payload = json.dumps(filters, sort_keys=True)
    cache_key = f"live_opportunities_{hashlib.sha256(cache_payload.encode('utf-8')).hexdigest()}"
    cached = cache.get(cache_key)
    if cached is None:
        cached = fetch_live_opportunities(filters)
        cache.set(cache_key, cached, 900)

    all_items = cached.get('items', [])
    total_count = len(all_items)
    start = (page - 1) * limit
    end = start + limit
    paginated_items = all_items[start:end]
    total_pages = max((total_count + limit - 1) // limit, 1) if total_count else 1

    return JsonResponse({
        'success': True,
        'opportunities': paginated_items,
        'source_notes': cached.get('source_notes', []),
        'fetched_at': cached.get('fetched_at'),
        'filters': filters,
        'meta': cached.get('meta', {}),
        'pagination': {
            'page': page,
            'limit': limit,
            'total_count': total_count,
            'total_pages': total_pages,
            'has_next': page < total_pages,
            'has_previous': page > 1,
        },
    })


@require_POST
def update_weekly_goal(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Authentication required'}, status=401)

    try:
        body = json.loads(request.body.decode('utf-8'))
        target_steps = int(body.get('target_steps', 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({'error': 'Enter a valid weekly goal.'}, status=400)

    if target_steps < 1:
        return JsonResponse({'error': 'Weekly goal must be at least 1 step.'}, status=400)

    week_start = _current_week_start()
    goal, _ = WeeklyGoal.objects.get_or_create(
        user=request.user,
        week_start=week_start,
        defaults={'target_steps': target_steps},
    )
    goal.target_steps = target_steps
    goal.save(update_fields=['target_steps', 'updated_at'])

    summary = _weekly_goal_summary(request.user)
    return JsonResponse({
        'success': True,
        'target_steps': goal.target_steps,
        'completed_this_week': summary['completed_this_week'],
        'remaining_steps': summary['remaining_steps'],
        'completion_percentage': summary['completion_percentage'],
    })


@require_POST
def toggle_roadmap_step_progress(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Authentication required'}, status=401)

    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)

    snapshot_id = body.get('snapshot_id')
    step_index = body.get('step_index')

    if snapshot_id in (None, '') or step_index in (None, ''):
        return JsonResponse({'error': 'Snapshot and step are required'}, status=400)

    snapshot = get_object_or_404(RoadmapSnapshot, pk=snapshot_id, user=request.user)

    try:
        step_index = int(step_index)
    except (TypeError, ValueError):
        return JsonResponse({'error': 'Invalid step index'}, status=400)

    steps = snapshot.payload.get('roadmap', {}).get('steps', [])
    if step_index < 0 or step_index >= len(steps):
        return JsonResponse({'error': 'Step index out of range'}, status=400)

    progress, created = RoadmapStepProgress.objects.get_or_create(
        user=request.user,
        snapshot=snapshot,
        step_index=step_index,
    )
    if not created:
        progress.delete()
    else:
        LearningActivity.objects.create(
            user=request.user,
            activity_type='roadmap_step_completed',
            metadata={
                'snapshot_id': snapshot.pk,
                'snapshot_title': snapshot.title,
                'step_index': step_index,
            },
        )

    total_steps = len(steps)
    completed_steps = snapshot.step_progress.count()
    completion_percentage = round((completed_steps / total_steps) * 100) if total_steps else 0
    weekly_goal = _weekly_goal_summary(request.user)
    streak_days = _current_streak_days(request.user)

    return JsonResponse({
        'completed': created,
        'snapshot_id': snapshot.pk,
        'step_index': step_index,
        'completed_steps': completed_steps,
        'total_steps': total_steps,
        'completion_percentage': completion_percentage,
        'weekly_completed_steps': weekly_goal['completed_this_week'],
        'weekly_target_steps': weekly_goal['goal'].target_steps,
        'weekly_completion_percentage': weekly_goal['completion_percentage'],
        'streak_days': streak_days,
    })


@require_POST
def save_issue_snapshot(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Authentication required'}, status=401)

    try:
        body = json.loads(request.body.decode('utf-8'))
        issue = (body.get('issue') or '').strip()
        guidance = body.get('guidance') or {}
    except Exception:
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)

    if not issue or not isinstance(guidance, dict):
        return JsonResponse({'error': 'Issue guidance data is required'}, status=400)

    payload = {
        'issue': issue,
        'guidance': guidance,
    }
    fingerprint = _build_snapshot_fingerprint(payload)
    snapshot = IssueAssistanceSnapshot.objects.filter(user=request.user, fingerprint=fingerprint).first()

    if snapshot:
        SavedItem.objects.filter(
            user=request.user,
            content_type=ContentType.objects.get_for_model(IssueAssistanceSnapshot),
            object_id=snapshot.pk,
        ).delete()
        snapshot.delete()
        saved = False
    else:
        summary = _clean_text(guidance.get('simple_explanation', ''))[:500]
        snapshot = IssueAssistanceSnapshot.objects.create(
            user=request.user,
            title=guidance.get('issue_detected') or issue[:120] or 'Issue assistance',
            issue=issue,
            summary=summary,
            payload=payload,
            fingerprint=fingerprint,
        )
        SavedItem.objects.create(
            user=request.user,
            content_type=ContentType.objects.get_for_model(IssueAssistanceSnapshot),
            object_id=snapshot.pk,
        )
        saved = True

    return JsonResponse({
        'saved': saved,
        'saved_count': SavedItem.objects.filter(user=request.user).count(),
        'detail_url': snapshot.get_absolute_url() if saved else '',
    })


@require_POST
def save_company_prep_snapshot(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Authentication required'}, status=401)

    try:
        body = json.loads(request.body.decode('utf-8'))
        company = (body.get('company') or '').strip()
        role = (body.get('role') or '').strip()
        plan_text = (body.get('plan_text') or '').strip()
        prep_inputs = body.get('prep_inputs') or {}
    except Exception:
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)

    if not company or not role or not plan_text:
        return JsonResponse({'error': 'Company preparation data is required'}, status=400)

    payload = {
        'company': company,
        'role': role,
        'plan_text': plan_text,
        'prep_inputs': prep_inputs,
    }
    fingerprint = _build_snapshot_fingerprint(payload)
    snapshot = CompanyPrepSnapshot.objects.filter(user=request.user, fingerprint=fingerprint).first()

    if snapshot:
        SavedItem.objects.filter(
            user=request.user,
            content_type=ContentType.objects.get_for_model(CompanyPrepSnapshot),
            object_id=snapshot.pk,
        ).delete()
        snapshot.delete()
        saved = False
    else:
        first_section = plan_text.split('\n\n', 1)[0]
        snapshot = CompanyPrepSnapshot.objects.create(
            user=request.user,
            title=f'{company} - {role}',
            company=company,
            role=role,
            summary=_clean_text(first_section)[:500],
            payload=payload,
            fingerprint=fingerprint,
        )
        SavedItem.objects.create(
            user=request.user,
            content_type=ContentType.objects.get_for_model(CompanyPrepSnapshot),
            object_id=snapshot.pk,
        )
        saved = True

    return JsonResponse({
        'saved': saved,
        'saved_count': SavedItem.objects.filter(user=request.user).count(),
        'detail_url': snapshot.get_absolute_url() if saved else '',
    })


@require_POST
def save_career_path(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Authentication required'}, status=401)

    try:
        body = json.loads(request.body.decode('utf-8'))
        career = body.get('career') or {}
    except Exception:
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)

    if not isinstance(career, dict) or not career.get('career_name'):
        return JsonResponse({'error': 'Career path data is required'}, status=400)

    payload = {
        'career': career,
    }
    fingerprint = _build_snapshot_fingerprint(payload)
    snapshot = CareerPathSnapshot.objects.filter(user=request.user, fingerprint=fingerprint).first()

    if snapshot:
        SavedItem.objects.filter(
            user=request.user,
            content_type=ContentType.objects.get_for_model(CareerPathSnapshot),
            object_id=snapshot.pk,
        ).delete()
        snapshot.delete()
        saved = False
    else:
        summary = _clean_text(career.get('simple_explanation', ''))[:500]
        snapshot = CareerPathSnapshot.objects.create(
            user=request.user,
            title=career['career_name'],
            summary=summary,
            payload=payload,
            fingerprint=fingerprint,
        )
        SavedItem.objects.create(
            user=request.user,
            content_type=ContentType.objects.get_for_model(CareerPathSnapshot),
            object_id=snapshot.pk,
        )
        saved = True

    return JsonResponse({
        'saved': saved,
        'saved_count': SavedItem.objects.filter(user=request.user).count(),
        'detail_url': snapshot.get_absolute_url() if saved else '',
    })


@require_POST
def save_roadmap_snapshot(request):
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Authentication required'}, status=401)

    try:
        body = json.loads(request.body.decode('utf-8'))
        prompt = (body.get('prompt') or '').strip()
        roadmap = body.get('roadmap') or {}
    except Exception:
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)

    if not prompt or not isinstance(roadmap, dict) or not roadmap.get('roadmap_title'):
        return JsonResponse({'error': 'Roadmap data is required'}, status=400)

    payload = {
        'prompt': prompt,
        'roadmap': roadmap,
    }
    fingerprint = _build_snapshot_fingerprint(payload)
    snapshot = RoadmapSnapshot.objects.filter(user=request.user, fingerprint=fingerprint).first()

    if snapshot:
        SavedItem.objects.filter(
            user=request.user,
            content_type=ContentType.objects.get_for_model(RoadmapSnapshot),
            object_id=snapshot.pk,
        ).delete()
        snapshot.delete()
        saved = False
    else:
        first_step = ''
        if roadmap.get('steps'):
            first_step = roadmap['steps'][0].get('description', '')
        snapshot = RoadmapSnapshot.objects.create(
            user=request.user,
            title=roadmap['roadmap_title'],
            prompt=prompt,
            summary=_clean_text(first_step)[:500],
            payload=payload,
            fingerprint=fingerprint,
        )
        SavedItem.objects.create(
            user=request.user,
            content_type=ContentType.objects.get_for_model(RoadmapSnapshot),
            object_id=snapshot.pk,
        )
        saved = True

    return JsonResponse({
        'saved': saved,
        'saved_count': SavedItem.objects.filter(user=request.user).count(),
        'detail_url': snapshot.get_absolute_url() if saved else '',
    })


@login_required(login_url='techbrat:signin')
def course_detail(request, pk):
    course = get_object_or_404(Course, pk=pk)
    return _render_resource_detail(
        request,
        course,
        'course',
        course.title,
        course.description,
        course.link,
        [
            ('Platform', course.platform),
            ('Domain', course.domain),
            ('Level', course.get_level_display()),
            ('Learning Type', course.get_learning_type_display()),
            ('Duration', course.duration),
            ('Pricing', 'Free' if course.is_free else 'Paid'),
        ],
    )


@login_required(login_url='techbrat:signin')
def book_detail(request, pk):
    book = get_object_or_404(Book, pk=pk)
    return _render_resource_detail(
        request,
        book,
        'book',
        book.title,
        book.description,
        book.link,
        [
            ('Author', book.author),
            ('Domain', book.domain),
            ('Level', book.get_level_display()),
            ('Type', book.get_book_type_display()),
        ],
    )


@login_required(login_url='techbrat:signin')
def tool_detail(request, pk):
    tool = get_object_or_404(Tool, pk=pk)
    return _render_resource_detail(
        request,
        tool,
        'tool',
        tool.name,
        tool.description,
        tool.link,
        [
            ('Category', tool.get_category_display()),
            ('Difficulty', tool.get_difficulty_display()),
            ('Use Case', tool.get_use_case_display()),
        ],
    )


@login_required(login_url='techbrat:signin')
def tip_detail(request, pk):
    tip = get_object_or_404(Tip, pk=pk)
    return _render_resource_detail(
        request,
        tip,
        'tip',
        tip.title,
        tip.explanation,
        '',
        [
            ('Category', tip.get_category_display()),
            ('Action Step', tip.action_step),
        ],
    )


@login_required(login_url='techbrat:signin')
def career_path_detail(request, pk):
    snapshot = get_object_or_404(CareerPathSnapshot, pk=pk, user=request.user)
    career = snapshot.payload.get('career', {})
    return render(request, 'career_path_detail.html', {
        'snapshot': snapshot,
        'career': career,
    })


@login_required(login_url='techbrat:signin')
def roadmap_snapshot_detail(request, pk):
    snapshot = get_object_or_404(RoadmapSnapshot, pk=pk, user=request.user)
    roadmap = snapshot.payload.get('roadmap', {})
    steps = _roadmap_steps_with_progress(snapshot)
    total_steps = len(steps)
    completed_steps = sum(1 for step in steps if step['is_completed'])
    return render(request, 'roadmap_snapshot_detail.html', {
        'snapshot': snapshot,
        'roadmap': roadmap,
        'steps': steps,
        'completed_steps': completed_steps,
        'total_steps': total_steps,
        'completion_percentage': round((completed_steps / total_steps) * 100) if total_steps else 0,
    })


@login_required(login_url='techbrat:signin')
def issue_snapshot_detail(request, pk):
    snapshot = get_object_or_404(IssueAssistanceSnapshot, pk=pk, user=request.user)
    guidance = snapshot.payload.get('guidance', {})
    return render(request, 'issue_snapshot_detail.html', {
        'snapshot': snapshot,
        'guidance': guidance,
        'issue_text': snapshot.issue,
    })


@login_required(login_url='techbrat:signin')
def company_prep_detail(request, pk):
    snapshot = get_object_or_404(CompanyPrepSnapshot, pk=pk, user=request.user)
    return render(request, 'company_prep_detail.html', {
        'snapshot': snapshot,
        'plan_text': snapshot.payload.get('plan_text', ''),
    })

def signin(request):
    if request.user.is_authenticated:
        return redirect(_get_safe_next_url(request, reverse('techbrat:welcome')))
    
    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')
        
        # Check if user exists
        try:
            user_exists = User.objects.get(email=email)
            # User exists, authenticate
            user = authenticate(request, username=user_exists.username, password=password)
            
            if user is not None:
                login(request, user)
                return redirect(_get_safe_next_url(request, reverse('techbrat:welcome')))
            else:
                # User exists but password is wrong
                messages.error(request, 'Incorrect password. Please try again.')
        except User.DoesNotExist:
            # User doesn't exist
            messages.error(request, 'Email not found. Please create an account to continue.')
    
    return render(request, 'landing_auth.html', get_landing_context(request, 'signin'))

def signup(request):
    if request.user.is_authenticated:
        return redirect(_get_safe_next_url(request, reverse('techbrat:welcome')))
    
    if request.method == 'POST':
        fullname = request.POST.get('fullname')
        email = request.POST.get('email')
        password = request.POST.get('password')
        confirm_password = request.POST.get('confirm_password')
        
        # Validation
        if password != confirm_password:
            messages.error(request, 'Passwords do not match.')
            return render(request, 'landing_auth.html', get_landing_context(request, 'signup'))
        
        if User.objects.filter(email=email).exists():
            messages.error(request, 'Email already registered.')
            return render(request, 'landing_auth.html', get_landing_context(request, 'signup'))
        
        if len(password) < 8:
            messages.error(request, 'Password must be at least 8 characters long.')
            return render(request, 'landing_auth.html', get_landing_context(request, 'signup'))
        
        # Create new user
        try:
            user = User.objects.create_user(
                username=email,
                email=email,
                password=password,
                first_name=fullname
            )
            user.save()

            # Ensure profile exists immediately for downstream views/templates
            get_or_create_profile(user)

            # Auto-login after signup
            login(request, user)
            return redirect(_get_safe_next_url(request, reverse('techbrat:welcome')))
        except Exception as e:
            messages.error(request, f'Error creating account: {str(e)}')
    
    return render(request, 'landing_auth.html', get_landing_context(request, 'signup'))


def social_login_error_redirect(request):
    messages.error(
        request,
        'Social sign-in could not be completed. Please try again from this page.',
    )
    return redirect('techbrat:signin')


def social_login_cancelled_redirect(request):
    messages.info(request, 'Social sign-in was cancelled. You can try again here.')
    return redirect('techbrat:signin')


def welcome(request):
    if not request.user.is_authenticated:
        return redirect('techbrat:signin')
    return render(request, 'welcome.html')

def signout(request):
    logout(request)
    messages.success(request, 'You have been signed out successfully!')
    return redirect('techbrat:index')


def get_or_create_profile(user):
    """Centralized helper to safely fetch or create a profile with sane defaults."""
    profile, _ = UserProfile.objects.get_or_create(user=user)

    if not profile.education_level:
        profile.education_level = 'undergraduate'
        profile.save(update_fields=['education_level'])

    return profile


def get_user_domains(profile):
    """Return tech domains as a trimmed list for template-friendly rendering."""
    if not profile.tech_domains:
        return []
    return [d.strip() for d in profile.tech_domains.split(',') if d.strip()]


def get_profile_completion(profile):
    """Calculate profile completion in 20% steps based on key fields and skills."""
    fields = [
        profile.current_course,
        profile.year_of_study,
        profile.tech_domains,
        profile.target_timeline,
    ]

    completion = sum(1 for field in fields if field) * 20

    if profile.skills.exists():
        completion += 20

    return min(completion, 100)
def get_user_level(user):
    """
    Determines user experience level (beginner, intermediate, advanced)
    based on their profile and activity.
    """
    if not user.is_authenticated:
        return "beginner"
    
    from .models import UserProfile, UserSkill
    
    try:
        profile = user.profile
        
        # 1. Check education level
        if profile.education_level in ['postgraduate', 'doctorate']:
            base_score = 2
        elif profile.education_level == 'undergraduate':
            base_score = 1
        else:
            base_score = 0
            
        # 2. Check skill levels
        skills = profile.skills.all()
        if skills.exists():
            advanced_skills = skills.filter(proficiency='advanced').count()
            intermediate_skills = skills.filter(proficiency='intermediate').count()
            
            if advanced_skills >= 2:
                base_score += 2
            elif intermediate_skills >= 3 or advanced_skills >= 1:
                base_score += 1
                
        
            
        # Final Determination
        if base_score >= 4:
            return "advanced"
        elif base_score >= 2:
            return "intermediate"
        else:
            return "beginner"
            
    except Exception:
        return "beginner"


# --------------------------- STATIC ROADMAP API ---------------------------

def get_roadmap(request):
    try:
        filepath = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "data",
            "backend_roadmap.json",
        )

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        return JsonResponse(data, safe=False)

    except FileNotFoundError:
        return JsonResponse({"error": "Roadmap JSON file not found"}, status=404)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

# ---------------------------
# Tech-domain validation helpers
# ---------------------------

NON_TECH_DOMAINS = [
    "dance", "dancing", "choreography",
    "singing", "music", "guitar", "piano",
    "cooking", "recipe", "chef",
    "painting", "sketching", "art",
    "sports", "football", "cricket", "tennis",
    "gym", "fitness", "yoga",
    "acting", "drama",
    "fashion", "makeup",
    "astrology", "horoscope",
    "politics", "religion"
]

def is_obviously_non_tech(text: str) -> bool:
    if not text:
        return False
    text = text.lower()
    return any(word in text for word in NON_TECH_DOMAINS)



def ai_is_tech_related(prompt: str) -> bool:
    """
    AI-based classifier.
    NEVER raises exception.
    Returns False if unsure.
    """
    try:
        response = post_openrouter(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.OPENROUTER_MODEL,
                "temperature": 0,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a strict classifier.\n"
                            "Answer ONLY with YES or NO.\n"
                            "Is the following query related to technology, "
                            "software development, computer science, or IT?"
                        )
                    },
                    {"role": "user", "content": prompt}
                ]
            },
            timeout=8
        )

        data = response.json()

        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
            .upper()
        )

        return content == "YES"

    except Exception as e:
        # 🔒 SAFETY NET
        print("AI classifier failed:", str(e))
        return False

# --------------------------- AI ROADMAP GENERATOR ---------------------------

@require_POST
def generate_roadmap(request):
    """
    Generates a technical roadmap using AI.
    Guarantees JSON-only response to frontend.
    """

    try:
        # -------------------------
        # Parse Request Body
        # -------------------------
        try:
            body = json.loads(request.body.decode("utf-8"))
        except Exception:
            return JsonResponse(
                {"success": False, "error": "Invalid JSON in request body"},
                status=400
            )

        prompt = body.get("prompt", "").strip()
        prompt = normalize_tech_query(prompt)
        if not prompt:
            return JsonResponse(
                {"success": False, "error": "Prompt is required"},
                status=400
            )
            
        # -------------------------
# Tech-only validation (STRICT)
# -------------------------

        if not is_tech_query(prompt):
            alt_msg = get_alternative_suggestions()
            return JsonResponse({
                "success": False,
                "error": f"Only technology-related content is supported. {alt_msg}."
            }, status=400)

        # -------------------------
        # Determine User Level
        # -------------------------
        user_level = get_user_level(request.user)

        # -------------------------
        # AI Request
        # -------------------------
        response = post_openrouter(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost",
                "X-Title": "TechBrat Roadmap Generator",
            },
            json={
                "model": settings.OPENROUTER_MODEL,
                "temperature": 0.4,
                "messages": [
                    {
                        "role": "system",
                        "content": f"""
You are an expert technical architect and educator.
Role: Generate a PRECISE and BROAD technical roadmap for a {user_level} learner.

CRITICAL RULES:
1. Content must be BROAD and EXPLAINED in great detail.
2. Formatting: Use PLAIN TEXT ONLY. Avoid any markdown symbols like **, ###, or triple backticks.
3. Structure: Present descriptions as a list of bullet points (one point per line starting with •).
4. Tailor the complexity and resources to a {user_level} level.
5. Return roadmap in valid JSON only.

STRICT JSON STRUCTURE (NO EXTRA TEXT):
{{
  "roadmap_title": "Comprehensive [Topic] Roadmap for {user_level}s",
  "steps": [
    {{
      "title": "Step Title",
      "description": "• Point 1: Broad detail.\\n• Point 2: Deep insight.\\n• Point 3: Practical application.",
      "resources": [
        {{
          "title": "Resource Name",
          "link": "Direct link to tutorial/documentation"
        }}
      ]
    }}
  ]
}}
"""
                    },
                    {"role": "user", "content": prompt}
                ],
            },
            timeout=25
        )

        # -------------------------
        # Parse AI Response
        # -------------------------
        try:
            raw = response.json()
        except Exception:
            return JsonResponse(
                {
                    "success": False,
                    "error": "AI returned non-JSON response",
                    "raw": response.text[:300],
                },
                status=500
            )

        if "choices" not in raw or not raw["choices"]:
            return JsonResponse(
                {
                    "success": False,
                    "error": "Invalid AI response structure",
                    "raw": raw,
                },
                status=500
            )

        try:
            content = raw["choices"][0]["message"]["content"]
            if not content:
                raise ValueError("Empty AI content")
            content = content.strip()
        except Exception:
            return JsonResponse(
                {
                    "success": False,
                    "error": "AI returned empty or malformed content",
                    "raw": raw,
                },
                status=500
            )

        # -------------------------
        # Clean AI Output
        # -------------------------
        content = content.replace("```json", "").replace("```", "").strip()

        # Ensure JSON starts properly
        json_start = content.find("{")
        if json_start != -1:
            content = content[json_start:]

        # -------------------------
        # Final JSON Parse Guard
        # -------------------------
        try:
            roadmap = json.loads(content, strict=False)
        except Exception:
            return JsonResponse(
                {
                    "success": False,
                    "error": "AI produced invalid JSON",
                    "raw_output": content[:400],
                },
                status=500
            )

        # -------------------------
        # Validate JSON Structure
        # -------------------------
        if (
            not isinstance(roadmap, dict)
            or "roadmap_title" not in roadmap
            or "steps" not in roadmap
            or not isinstance(roadmap["steps"], list)
        ):
            return JsonResponse(
                {
                    "success": False,
                    "error": "Invalid roadmap format",
                    "roadmap": roadmap,
                },
                status=500
            )

        # -------------------------
        # SUCCESS RESPONSE
        # -------------------------
        return JsonResponse(
            {
                "success": True,
                "data": roadmap
            },
            status=200
        )

    except Exception as e:
        return JsonResponse(
            {
                "success": True,
                "data": _fallback_roadmap(prompt, user_level if 'user_level' in locals() else "beginner"),
                "fallback": True,
            },
            status=200
        )


@require_POST
def issue_assistance(request):
    try:
        body = json.loads(request.body.decode("utf-8"))
        issue = body.get("issue", "").strip()
        issue = normalize_tech_query(issue)

        if not issue:
            return JsonResponse(
                {"error": "Issue text is required"},
                status=400
            )

        # ------------------------------------------------
        # TECH-ONLY VALIDATION (SAME AS ROADMAP)
        # ------------------------------------------------

        if not is_tech_query(issue):
            alt_msg = get_alternative_suggestions()
            return JsonResponse({
                "success": False,
                "error": f"Only technology-related content is supported. {alt_msg}."
            }, status=400)

        # ------------------------------------------------
        # CONTINUE WITH AI ASSISTANCE
        # ------------------------------------------------

        user_name = (
            request.user.first_name
            if request.user.is_authenticated
            else "Student"
        )
        user_level = get_user_level(request.user)

        system_prompt = f"""
You are a senior tech mentor.
Role: Provide deep, broad, and beginner-friendly assistance for technical issues.
Target Level: {user_level}

CRITICAL RULES:
1. Provide VERY EXPLAINED and BROAD content.
2. Formatting: Use PLAIN TEXT ONLY. Avoid any markdown symbols.
3. Structure: Use bullet points (•), one point per line.
4. Focus strictly on TECHNOLOGY / PROGRAMMING issues.

Return ONLY valid JSON in this exact format:
{{
  "issue_detected": "Brief technical issue name",
  "simple_explanation": "• Point 1 explaining why it happens.\n• Point 2 explaining the concept.",
  "alternative_learning": "• Alternative approach 1.\n• Alternative approach 2.",
  "practice_or_example": "• Practice tip 1.\n• Example idea.",
  "motivation_boost": "Encouraging advice for a {user_level} learner."
}}
"""

        response = post_openrouter(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.OPENROUTER_MODEL,
                "temperature": 0.5,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": f"Student Name: {user_name}\nIssue: {issue}",
                    },
                ],
            },
            timeout=25,
        )

        raw = response.json()

        if "choices" not in raw or not raw["choices"]:
            return JsonResponse(
                {"error": "AI response failed", "details": raw},
                status=500
            )

        content = raw["choices"][0]["message"]["content"]
        content = content.replace("```json", "").replace("```", "").strip()

        return JsonResponse(
            json.loads(content, strict=False),
            status=200
        )

    except json.JSONDecodeError:
        return JsonResponse(
            _fallback_issue_response(issue, user_level if 'user_level' in locals() else "beginner"),
            status=200
        )

    except Exception as e:
        return JsonResponse(
            _fallback_issue_response(issue, user_level if 'user_level' in locals() else "beginner"),
            status=200
        )


@require_POST
def career_guidance(request):
    try:
        body = _safe_json_body(request)
        # prompt = body.get("prompt") # No longer receiving full prompt from frontend
        
        user_name = request.user.first_name if request.user.is_authenticated else "Student"
        user_level = get_user_level(request.user)
        education_level = body.get("education_level", "N/A")
        interests = body.get("interests", body.get("domain", "N/A"))
        skills = body.get("skills", "N/A")
        learning_style = body.get("learning_style", "N/A")
        career_goal = body.get("career_goal", body.get("goal", "N/A"))
        math_level = body.get("math_level", "N/A")
        logic_level = body.get("logic_level", "N/A")
        problem_solving = body.get("problem_solving", "N/A")
        background = body.get("background", "N/A")
        hours_per_week = body.get("hours_per_week", "N/A")
        timeline = body.get("timeline", "N/A")
        career_goals = body.get("career_goals", career_goal)
        work_preference = body.get("work_preference", "N/A")
        risk_appetite = body.get("risk_appetite", "N/A")
        preferred_work_style = body.get("preferred_work_style", "N/A")
        location_preference = body.get("location_preference", "N/A")

        profile_data = {
            "education_level": education_level,
            "interests": interests,
            "skills": skills,
            "learning_style": learning_style,
            "career_goal": career_goal,
            "math_level": math_level,
            "logic_level": logic_level,
            "problem_solving": problem_solving,
            "background": background,
            "hours_per_week": hours_per_week,
            "timeline": timeline,
            "career_goals": career_goals,
            "work_preference": work_preference,
            "risk_appetite": risk_appetite,
            "preferred_work_style": preferred_work_style,
            "location_preference": location_preference,
        }
        # -------------------------
# TECH-ONLY VALIDATION
# -------------------------

        combined_input = f"{interests} {skills} {career_goal} {background} {career_goals}".strip()
        combined_input = normalize_tech_query(combined_input)

        if not is_tech_query(combined_input):
            alt_msg = get_alternative_suggestions()
            return JsonResponse({
                "success": False,
                "error": f"Only technology-related content is supported. {alt_msg}."
            }, status=400)

        system_prompt = f"You are an expert tech career advisor for a {user_level} learner. Return only JSON."
        prompt = CAREER_PATH_PROMPT.format(
            interest=interests,
            math_level=math_level,
            logic_level=logic_level,
            problem_solving=problem_solving,
            skills=skills,
            background=background,
            hours_per_week=hours_per_week,
            timeline=timeline,
            career_goals=career_goals,
            work_preference=work_preference,
            risk_appetite=risk_appetite,
            preferred_work_style=preferred_work_style,
            location_preference=location_preference,
        ) + f"\n\nAssessed learner level: {user_level}\nUser name: {user_name}\nPreferred learning style: {learning_style}\nEducation level: {education_level}\n"

        response = post_openrouter(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.OPENROUTER_MODEL,
                "temperature": 0.5,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
            },
        )

        raw = response.json()

        if "choices" not in raw:
            return JsonResponse({"error": "AI response failed", "details": raw}, status=500)

        content = raw["choices"][0]["message"]["content"]
        content = content.replace("```json", "").replace("```", "").strip()

        parsed = json.loads(content, strict=False)
        return JsonResponse(_normalize_career_response(parsed))


    except json.JSONDecodeError:
        return JsonResponse(_fallback_career_guidance(user_level, profile_data))

    except Exception as e:
        return JsonResponse(_fallback_career_guidance(user_level, profile_data))


@login_required(login_url='techbrat:signin')
def profile(request):
    """Display and manage user profile"""
    profile = get_or_create_profile(request.user)
    
    if request.method == 'POST':
        # Handle profile updates
        action = request.POST.get('action')
        
        if action == 'update_profile':
            # 1. Basic Academic Information
            profile.education_level = request.POST.get('education_level', profile.education_level)
            profile.current_course = request.POST.get('current_course', profile.current_course)
            profile.year_of_study = request.POST.get('year_of_study', profile.year_of_study)
            
            # 3. Domain Interests
            profile.tech_domains = request.POST.get('tech_domains', profile.tech_domains)
            profile.preferred_domain_work_type = request.POST.get('preferred_domain_work_type', profile.preferred_domain_work_type)
            
            # 4. Career Goals
            profile.career_objective = request.POST.get('career_objective', profile.career_objective)
            profile.target_timeline = request.POST.get('target_timeline', profile.target_timeline)
            profile.preferred_work_mode = request.POST.get('preferred_work_mode', profile.preferred_work_mode)
            
            profile.save()
            messages.success(request, 'Profile updated successfully!')
            return redirect('techbrat:profile')
        
        elif action == 'add_skill':
            skill_name = request.POST.get('skill_name')
            proficiency = request.POST.get('proficiency', 'intermediate')
            
            if skill_name:
                UserSkill.objects.get_or_create(
                    profile=profile,
                    skill_name=skill_name,
                    defaults={'proficiency': proficiency}
                )
                messages.success(request, 'Skill added successfully!')
            return redirect('techbrat:profile')
        
        elif action == 'delete_skill':
            skill_id = request.POST.get('skill_id')
            try:
                skill = UserSkill.objects.get(id=skill_id, profile=profile)
                skill.delete()
                messages.success(request, 'Skill removed successfully!')
            except UserSkill.DoesNotExist:
                pass
            return redirect('techbrat:profile')
        
    # Get related data and calculate completion percentage
    skills = profile.skills.all()
    completion = get_profile_completion(profile)
    readiness = _job_readiness_summary(request.user)
    skill_gap = _skill_gap_summary(request.user)
    action_plan = _personalized_action_plan(request.user)
    project_guidance = _project_guidance_summary(request.user)
    
    context = {
        'profile': profile,
        'user': request.user,
        'skills': skills,
        'domains_list': get_user_domains(profile),
        'completion_percentage': completion,
        'education_level_choices': UserProfile.EDUCATION_LEVEL_CHOICES,
        'domain_work_type_choices': UserProfile.DOMAIN_WORK_TYPE_CHOICES,
        'career_objective_choices': UserProfile.CAREER_OBJECTIVE_CHOICES,
        'work_mode_choices': UserProfile.WORK_MODE_CHOICES,
        'skill_level_choices': UserSkill.SKILL_LEVEL_CHOICES,
        'readiness': readiness,
        'skill_gap': skill_gap,
        'action_plan': action_plan,
        'project_guidance': project_guidance,
    }
    
    return render(request, 'profile.html', context)


@login_required(login_url='techbrat:signin')
def courses(request):
    """
    Displays personalized course recommendations based on user profile and filters.
    """
    # Get user level and domains
    user_level = get_user_level(request.user)
    profile = get_or_create_profile(request.user)
    user_domains = get_user_domains(profile)
    
    # Base Queryset
    queryset = Course.objects.only(
        'id',
        'title',
        'platform',
        'level',
        'domain',
        'is_free',
        'learning_type',
        'duration',
        'link',
        'description',
    )
    
    # Apply Personalization (Default behavior if no filters provided)
    level_filter = request.GET.get('level')
    domain_filter = request.GET.get('domain')
    free_filter = request.GET.get('free')
    
    is_filtered = any([level_filter, domain_filter, free_filter])
    
    if not is_filtered:
        # Default: Filter by user level
        queryset = queryset.filter(level=user_level)
        # If user has domains, try to match them
        if user_domains:
            # Simple intersection: show courses matching at least one domain
            from django.db.models import Q
            domain_query = Q()
            for domain in user_domains:
                domain_query |= Q(domain__icontains=domain)
            queryset = queryset.filter(domain_query)
    else:
        # Apply manual filters
        if level_filter:
            queryset = queryset.filter(level=level_filter)
        if domain_filter:
            queryset = queryset.filter(domain__icontains=domain_filter)
        if free_filter:
            is_free = free_filter.lower() == 'true'
            queryset = queryset.filter(is_free=is_free)
            
    # Get all unique domains for the filter dropdown
    all_domains = cache.get('course_filter_domains')
    if all_domains is None:
        all_domains = list(
            Course.objects.exclude(domain='')
            .values_list('domain', flat=True)
            .distinct()
        )
        cache.set('course_filter_domains', all_domains, 900)

    initial_courses = list(queryset.order_by('title')[:6])
    
    context = {
        'courses': initial_courses,
        'saved_course_ids': set(
            SavedItem.objects.filter(
                user=request.user,
                content_type=ContentType.objects.get_for_model(Course),
            ).values_list('object_id', flat=True)
        ) if request.user.is_authenticated else set(),
        'user_level': user_level,
        'domains': profile.tech_domains,
        'all_domains': all_domains,
        'selected_level': level_filter or (user_level if not is_filtered else ""),
        'selected_domain': domain_filter,
        'selected_free': free_filter,
        'is_personalized': not is_filtered
    }
    
    return render(request, 'courses.html', context)


@login_required(login_url='techbrat:signin')
def books(request):
    profile = get_or_create_profile(request.user)
    user_level = get_user_level(request.user)
    domains_list = get_user_domains(profile)
    goal = profile.get_career_objective_display()

    context = {
        'user_level': user_level,
        'domains_list': domains_list,
        'goal': goal,
        'saved_book_ids': list(
            SavedItem.objects.filter(
                user=request.user,
                content_type=ContentType.objects.get_for_model(Book),
            ).values_list('object_id', flat=True)
        ),
        'level_choices': Book.LEVEL_CHOICES,
        'type_choices': Book.TYPE_CHOICES,
    }
    return render(request, 'books.html', context)


def _dedupe_books(books):
    seen = set()
    unique = []
    for b in books:
        if isinstance(b, Book):
            title = b.title
            author = b.author
        else:
            title = b['title']
            author = b['author']
        key = (title.strip().lower(), author.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(b)
    return unique


def _ai_books(level, domains, goal):
    from django.conf import settings
    prompt = f"""
You are a tech learning advisor.

Recommend 5 high-quality REAL books based on:

User Level: {level}
Domains: {domains}
Career Goal: {goal}

IMPORTANT:
- ONLY tech-related books
- NO non-technical topics
- If domain is niche, return related tech books

Each book must include:
- title
- author
- level
- type (theory/practical/interview)
- reason (why recommended)
- link

Return ONLY JSON array.
"""

    try:
        response = post_openrouter(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.OPENROUTER_MODEL,
                "temperature": 0.4,
                "messages": [
                    {"role": "system", "content": "Return only JSON array of books."},
                    {"role": "user", "content": prompt}
                ],
            },
            timeout=20,
        )
        raw = response.json()
        content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
        content = content.replace("```json", "").replace("```", "").strip()
        data = json.loads(content, strict=False)
        books = []
        for item in data:
            title = item.get("title", "").strip()
            author = item.get("author", "").strip()
            if not title or not author:
                continue

            book, _ = Book.objects.get_or_create(
                title=title,
                author=author,
                defaults={
                    "domain": domains if isinstance(domains, str) else ", ".join(domains),
                    "level": item.get("level", "beginner"),
                    "book_type": item.get("type", "theory"),
                    "description": item.get("reason", ""),
                    "link": item.get("link", ""),
                    "is_ai_generated": True,
                }
            )
            books.append(book)
        return books
    except Exception:
        return []


def _fallback_books(level, domains):
    queryset = Book.objects.all()
    if level:
        queryset = queryset.filter(level=level)
    if domains:
        queryset = queryset.filter(domain__icontains=domains.split(',')[0].strip())

    books = list(queryset[:5])

    if books:
        return books

    topic = domains or "Technology"
    book, _ = Book.objects.get_or_create(
        title=f"Introduction to {topic}",
        author="TechBrat",
        defaults={
            "domain": topic,
            "level": level or "beginner",
            "book_type": "theory",
            "description": f"Start with foundational learning resources for {topic}.",
            "link": f"https://www.google.com/search?q={topic.replace(' ', '+')}+best+books",
            "is_ai_generated": True,
        }
    )
    return [book]


def _fallback_issue_response(issue, user_level):
    return {
        "issue_detected": issue or "Technical issue",
        "simple_explanation": "• The problem is likely caused by configuration, input, or environment mismatch.\n• Isolate the failing step and verify the exact error message before changing code.",
        "alternative_learning": "• Reproduce the issue in the smallest possible example.\n• Check the framework docs for the failing feature and compare expected inputs.",
        "practice_or_example": "• Add logging around the failing code path.\n• Test one variable at a time so you can see what actually changes the result.",
        "motivation_boost": f"Debugging is a normal part of growth. A {user_level} learner improves fastest by turning one confusing error into one clear lesson."
    }


def _fallback_roadmap(prompt, user_level):
    topic = prompt or "Technology"
    return {
        "roadmap_title": f"Comprehensive {topic.title()} Roadmap for {user_level}s",
        "steps": [
            {
                "title": "Build Core Fundamentals",
                "description": "• Learn the basic concepts, terminology, and workflow for the topic.\n• Focus on one primary language, tool, or framework before branching out.\n• Practice with small examples until the basics feel natural.",
                "resources": [
                    {"title": "Official Documentation", "link": "https://developer.mozilla.org/"},
                    {"title": "Beginner Video Search", "link": f"https://www.youtube.com/results?search_query={topic.replace(' ', '+')}+beginner"}
                ]
            },
            {
                "title": "Apply Through Projects",
                "description": "• Build at least two small projects that use the topic in a practical way.\n• Debug your own mistakes and document what you learn.\n• Move from tutorial-following to independent building as early as possible.",
                "resources": [
                    {"title": "Project Ideas", "link": f"https://www.google.com/search?q={topic.replace(' ', '+')}+project+ideas"}
                ]
            },
            {
                "title": "Advance and Specialize",
                "description": "• Learn best practices, performance, testing, and deployment for the topic.\n• Read real-world code or case studies to improve your engineering judgment.\n• Choose one specialization path based on your goals and keep iterating.",
                "resources": [
                    {"title": "Advanced Learning Search", "link": f"https://www.google.com/search?q={topic.replace(' ', '+')}+advanced+tutorial"}
                ]
            }
        ]
    }


def _fallback_career_guidance(user_level, profile):
    focus = profile.get('interests') or "software development"
    goal = profile.get('career_goals') or profile.get('career_goal') or "a tech role"
    logic = (profile.get('logic_level') or '').lower()
    math_level = (profile.get('math_level') or '').lower()
    work_preference = (profile.get('work_preference') or '').lower()

    primary = "Backend Developer"
    alternative = "Full Stack Developer"

    if 'high' in math_level or 'data' in focus.lower() or 'analytics' in focus.lower():
        primary = "Data Analyst"
        alternative = "AI/ML Engineer"
    elif 'design' in focus.lower() or 'ui' in focus.lower() or 'frontend' in focus.lower():
        primary = "Frontend Developer"
        alternative = "Full Stack Developer"
    elif 'cloud' in focus.lower() or 'devops' in focus.lower() or 'automation' in focus.lower():
        primary = "DevOps Engineer"
        alternative = "Backend Developer"
    elif 'testing' in focus.lower() or 'qa' in focus.lower():
        primary = "QA/Software Tester"
        alternative = "Backend Developer"
    elif 'high' in logic and 'remote' in work_preference:
        primary = "Backend Developer"
        alternative = "DevOps Engineer"

    return {
        "recommended_careers": [
            {
                "career_name": primary,
                "confidence_score": 78,
                "simple_explanation": f"{primary} is a strong match for learners who want structured technical growth and clear problem-solving milestones.",
                "why_suitable": f"Your interest in {focus}, current skills, and goal of {goal} align well with the day-to-day work in {primary}. The recommendation also fits a {user_level} learner who wants a practical path forward.",
                "alternative_career_path": alternative,
                "future_scope": f"{primary} remains a practical path with room to grow into stronger engineering ownership, especially as your skills deepen.",
                "beginner_roadmap": [
                    "Step 1 (Beginner): Build foundations with one core language, basic tooling, and small hands-on exercises.",
                    "Step 2 (Intermediate): Create portfolio projects that reflect your target role and solve realistic problems.",
                    "Step 3 (Advanced): Strengthen deployment, debugging, collaboration, and interview-ready project depth."
                ],
                "key_skills": ["Programming fundamentals", "Problem solving", "Debugging", "Version control", "Project building"],
                "beginner_resources": [
                    {"title": "Django Documentation", "type": "Documentation", "link": "https://docs.djangoproject.com/"},
                    {"title": "MDN Web Docs", "type": "Documentation", "link": "https://developer.mozilla.org/"}
                ]
            }
        ],
        "fallback": True,
        "level": user_level,
    }


def _safe_json_body(request):
    try:
        return json.loads(request.body.decode("utf-8"))
    except Exception:
        return {}


CAREER_PATH_PROMPT = """
You are an expert tech career advisor.

A user has provided the following details. Based on ALL the inputs, suggest the most suitable tech career path.

User Profile:

1. Interest Area:
{interest}

2. Aptitude:
- Math Level: {math_level}
- Logical Thinking: {logic_level}
- Problem Solving: {problem_solving}

3. Current Skills:
{skills}

4. Background:
{background}

5. Time Commitment:
- Hours per week: {hours_per_week}
- Target timeline: {timeline}

6. Career Goals:
{career_goals}

7. Work Preference:
{work_preference}

8. Risk Appetite:
{risk_appetite}

9. Preferred Work Style:
{preferred_work_style}

10. Location Preference:
{location_preference}

Instructions:

1. Analyze the user holistically.
2. Recommend ONE primary career path from:
   - Frontend Developer
   - Backend Developer
   - Full Stack Developer
   - Data Analyst
   - AI/ML Engineer
   - DevOps Engineer
   - QA/Software Tester
3. Also suggest ONE alternative career path.
4. Provide a confidence score from 0 to 100.
5. Explain why the primary path fits the user.
6. Provide a short roadmap with exactly 3 steps:
   - Step 1 (Beginner)
   - Step 2 (Intermediate)
   - Step 3 (Advanced)
7. Mention key skills required.
8. Keep the answer concise but insightful.

Return ONLY valid JSON in this exact format:
{{
  "recommended_careers": [
    {{
      "career_name": "Backend Developer",
      "confidence_score": 86,
      "simple_explanation": "Short summary of what this role does.",
      "why_suitable": "Concise explanation of why this user is a strong fit.",
      "alternative_career_path": "Full Stack Developer",
      "future_scope": "Short note about growth and opportunity.",
      "beginner_roadmap": [
        "Step 1 (Beginner): ...",
        "Step 2 (Intermediate): ...",
        "Step 3 (Advanced): ..."
      ],
      "key_skills": ["Skill 1", "Skill 2", "Skill 3"],
      "beginner_resources": [
        {{ "title": "Resource Name", "type": "Course", "link": "https://example.com" }}
      ]
    }}
  ]
}}
"""


def _normalize_career_response(payload):
    careers = payload.get('recommended_careers') or []
    normalized = []

    for career in careers[:1]:
        if not isinstance(career, dict):
            continue

        confidence = career.get('confidence_score', 0)
        if isinstance(confidence, str):
            confidence_text = confidence.strip().rstrip('%')
            confidence = int(confidence_text) if confidence_text.isdigit() else 0

        roadmap = career.get('beginner_roadmap') or career.get('roadmap') or []
        if isinstance(roadmap, str):
            roadmap = [line.strip() for line in roadmap.splitlines() if line.strip()]

        key_skills = career.get('key_skills') or []
        if isinstance(key_skills, str):
            key_skills = [part.strip() for part in key_skills.split(',') if part.strip()]

        normalized.append({
            'career_name': career.get('career_name', 'Tech Career Path'),
            'confidence_score': confidence,
            'simple_explanation': career.get('simple_explanation', ''),
            'why_suitable': career.get('why_suitable', ''),
            'alternative_career_path': career.get('alternative_career_path', ''),
            'future_scope': career.get('future_scope', ''),
            'beginner_roadmap': roadmap[:3],
            'key_skills': key_skills[:6],
            'beginner_resources': career.get('beginner_resources', []),
        })

    return {'recommended_careers': normalized}


@require_POST
def book_recommendations(request):
    try:
        body = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    level = body.get("level", "beginner")
    domains = body.get("domains", [])
    goal = body.get("goal", "")

    domain_text = ", ".join(domains) if isinstance(domains, list) else str(domains)
    if not is_tech_query(domain_text):
        return JsonResponse({"success": False, "error": "🚫 Only technology-related books are supported. Try topics like AI, Web Development, or Databases."}, status=400)

    saved_lookup = _saved_lookup_for_user(request.user)
    ai_books = _ai_books(level, domain_text, goal)
    if not ai_books:
        ai_books = _fallback_books(level, domain_text)
    return JsonResponse({
        "success": True,
        "data": [_serialize_book(book, saved_lookup) for book in ai_books],
    }, status=200)


def filter_books(request):
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "GET only"}, status=405)

    level = request.GET.get("level")
    domain = request.GET.get("domain", "").strip()
    book_type = request.GET.get("type")

    if domain and not is_tech_query(domain):
        return JsonResponse({"success": False, "error": "🚫 Only technology-related books are supported. Try topics like AI, Web Development, or Databases."}, status=400)

    queryset = Book.objects.all()
    if level:
        queryset = queryset.filter(level=level)
    if domain:
        queryset = queryset.filter(domain__icontains=domain)
    if book_type:
        queryset = queryset.filter(book_type=book_type)

    saved_lookup = _saved_lookup_for_user(request.user)
    db_books = list(queryset)

    combined = db_books
    if len(db_books) < 4:
        ai_books = _ai_books(level or "beginner", domain or "technology", "")
        combined = _dedupe_books(db_books + ai_books)

    serialized = [
        _serialize_book(book, saved_lookup) if isinstance(book, Book) else book
        for book in combined
    ]

    return JsonResponse({"success": True, "data": serialized}, status=200)
@login_required(login_url='techbrat:signin')
def filter_courses(request):
    from .models import Course
    from .course_fetcher import fetch_external_courses, deduplicate_courses
    from django.db.models import Q
    from django.core.cache import cache
    import json
    import requests
    from django.conf import settings

    level = request.GET.get('level', 'beginner')
    domain = request.GET.get('domain', '')
    domain = normalize_tech_query(domain)
    learning_type = request.GET.get('learning_type', 'video')
    free = request.GET.get('free', 'true')

    is_free_bool = (free.lower() == 'true')

    if not is_tech_query(domain):
        alt_msg = get_alternative_suggestions()
        return JsonResponse({
            "success": False,
            "error": f"🚫 Only technology-related courses are supported. {alt_msg}."
        }, status=400)

    # ─── Cache Check ───────────────────────────────────
    cache_key = f"courses_{level}_{domain}_{learning_type}_{free}"
    cached = cache.get(cache_key)
    if cached is not None:
        saved_lookup = _saved_lookup_for_user(request.user)
        cached_with_state = []
        course_content_type = ContentType.objects.get_for_model(Course)
        saved_ids = {object_id for content_type_id, object_id in saved_lookup if content_type_id == course_content_type.id}
        for course in cached:
            item = dict(course)
            item['saved'] = bool(item.get('id') and item['id'] in saved_ids)
            cached_with_state.append(item)
        return JsonResponse({'success': True, 'courses': cached_with_state})

    # ─── Helper: serialize a DB queryset to list[dict] ──
    def serialize_queryset(qs, limit=15):
        result = []
        for course in qs[:limit]:
            serialized = _serialize_course(course)
            serialized['saved'] = False
            result.append(serialized)
        return result

    # ─── Helper: flexible domain filter (supports multi-word) ──
    domain_list = []
    if domain:
        domain_list = [d.strip() for d in domain.split(',') if d.strip()]

    def apply_domain_filter(qs):
        if domain_list:
            dq = Q()
            for d in domain_list:
                dq |= Q(domain__icontains=d)
                # Also search within title for broader matching
                dq |= Q(title__icontains=d)
            return qs.filter(dq)
        return qs

    # ─── STEP 1: Database Query (existing) ─────────────
    queryset = Course.objects.filter(
        level=level,
        learning_type=learning_type,
        is_free=is_free_bool
    )
    queryset = apply_domain_filter(queryset)
    db_courses = serialize_queryset(queryset, limit=15)

    # ─── STEP 2: External Fetch ────────────────────────
    # Trigger when DB returned < 6 results OR no results at all
    external_courses_raw = []
    if len(db_courses) < 6 or not queryset.exists():
        try:
            external_courses_raw = fetch_external_courses(
                level=level,
                domain=domain or 'technology',
                learning_type=learning_type,
                is_free=is_free_bool
            )

            # Save external courses to DB for future queries
            for course_data in external_courses_raw:
                if not Course.objects.filter(
                    title=course_data['title'],
                    platform=course_data['platform']
                ).exists():
                    try:
                        Course.objects.create(
                            title=course_data['title'],
                            platform=course_data['platform'],
                            domain=course_data.get('domain', ''),
                            level=course_data.get('level', level),
                            learning_type=course_data.get('learning_type', learning_type),
                            duration=course_data.get('duration', ''),
                            is_free=course_data.get('is_free', is_free_bool),
                            is_ai_generated=False,
                            is_external=True,
                            link=course_data.get('link', ''),
                            description=course_data.get('description', '')
                        )
                    except Exception:
                        continue

        except Exception as e:
            print(f"External course fetch failed: {e}")

    # ─── STEP 3: AI Fallback (enhanced) ────────────────
    # Re-query DB to include any newly saved external courses
    queryset = Course.objects.filter(
        level=level,
        learning_type=learning_type,
        is_free=is_free_bool
    )
    queryset = apply_domain_filter(queryset)

    # Track AI-generated courses for fallback guarantee
    ai_courses_raw = []

    if queryset.count() < 6 or not queryset.exists():

        api_key = getattr(settings, 'OPENROUTER_API_KEY', None)
        model = getattr(settings, 'OPENROUTER_MODEL', 'google/gemma-4-26b-a4b-it:free')

        if api_key:

            free_or_paid = "Free" if is_free_bool else "Paid"
            domain_text = domain if domain else "general technology"

            prompt = f"""
You are a real-world tech course recommendation engine.

Your job is to find HIGH-QUALITY, REAL courses available on the internet.

User Requirements:
- Skill Level: {level}
- Domain: {domain_text}
- Learning Type: {learning_type}
- Pricing: {free_or_paid}

IMPORTANT RULES:
1. Return at least 6 courses ALWAYS. Never return an empty array.
2. Courses MUST be REAL and available online.
3. Prefer platforms:
   - Coursera
   - Udemy
   - FreeCodeCamp
   - edX
   - YouTube (for free courses)
   - Official documentation/tutorials
4. Links MUST be valid and usable.
5. DO NOT generate fake courses.
6. If exact domain courses are limited, return CLOSELY RELATED courses.
   Examples:
   - Redis → caching, backend optimization, databases
   - Kafka → streaming, distributed systems, event-driven architecture
   - Docker → containerization, DevOps, cloud deployment
   - GraphQL → API design, web services, backend development
7. You MUST always return data. An empty response is UNACCEPTABLE.

Return ONLY JSON ARRAY (no markdown):

[
  {{
    "title": "",
    "platform": "",
    "level": "{level}",
    "domain": "{domain_text}",
    "learning_type": "{learning_type}",
    "is_free": {str(is_free_bool).lower()},
    "duration": "",
    "description": "2-3 line explanation",
    "link": ""
  }}
]
"""

            try:
                response = post_openrouter(
                    url="https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}]
                    },
                    timeout=15
                )

                if response.status_code == 200:

                    ai_content = response.json()['choices'][0]['message']['content']
                    ai_content = ai_content.replace("```json", "").replace("```", "").strip()

                    # Find JSON array start
                    arr_start = ai_content.find("[")
                    if arr_start != -1:
                        ai_content = ai_content[arr_start:]

                    ai_data = json.loads(ai_content)

                    if isinstance(ai_data, list):
                        ai_courses_raw = ai_data
                    else:
                        ai_courses_raw = []

                    for course_data in ai_courses_raw:
                        if not isinstance(course_data, dict):
                            continue
                        if not course_data.get('title') or not course_data.get('platform'):
                            continue
                        if not Course.objects.filter(
                            title=course_data['title'],
                            platform=course_data['platform']
                        ).exists():
                            try:
                                Course.objects.create(
                                    title=course_data['title'],
                                    platform=course_data['platform'],
                                    domain=course_data.get('domain', domain_text),
                                    level=course_data.get('level', level),
                                    learning_type=course_data.get('learning_type', learning_type),
                                    duration=course_data.get('duration', ''),
                                    is_free=course_data.get('is_free', is_free_bool),
                                    is_ai_generated=True,
                                    is_external=False,
                                    link=course_data.get('link', ''),
                                    description=course_data.get('description', '')
                                )
                            except Exception:
                                continue

                    # Re-run query AFTER saving
                    queryset = Course.objects.filter(
                        level=level,
                        learning_type=learning_type,
                        is_free=is_free_bool
                    )
                    queryset = apply_domain_filter(queryset)

                    # 🔥 FIX 2: Relax domain filter if it yields 0 results
                    if queryset.count() == 0:
                        queryset = Course.objects.filter(
                            level=level,
                            learning_type=learning_type,
                            is_free=is_free_bool
                        )

            except Exception as e:
                print("AI fallback failed:", str(e))

                # ─── Retry with simpler prompt ─────────────
                try:
                    simple_prompt = f"Generate 6 {free_or_paid.lower()} beginner courses for {domain_text}. Return ONLY JSON array with fields: title, platform, level, domain, learning_type, is_free, duration, description, link. No markdown."
                    retry_resp = post_openrouter(
                        url="https://openrouter.ai/api/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": model,
                            "messages": [{"role": "user", "content": simple_prompt}]
                        },
                        timeout=15
                    )
                    if retry_resp.status_code == 200:
                        retry_content = retry_resp.json()['choices'][0]['message']['content']
                        retry_content = retry_content.replace("```json", "").replace("```", "").strip()
                        arr_start = retry_content.find("[")
                        if arr_start != -1:
                            retry_content = retry_content[arr_start:]
                        retry_data = json.loads(retry_content)
                        if isinstance(retry_data, list):
                            ai_courses_raw = retry_data
                except Exception as retry_err:
                    print("AI retry also failed:", str(retry_err))

    # ─── STEP 4: Merge & Deduplicate ───────────────────
    all_courses = serialize_queryset(queryset, limit=20)

    # Add any external courses that didn't get saved
    for ext in external_courses_raw:
        all_courses.append({
            'title': ext.get('title', ''),
            'platform': ext.get('platform', ''),
            'level': ext.get('level', level),
            'level_display': level.capitalize(),
            'domain': ext.get('domain', ''),
            'is_free': ext.get('is_free', is_free_bool),
            'learning_type': ext.get('learning_type', learning_type),
            'learning_type_display': learning_type.replace('_', ' ').title(),
            'duration': ext.get('duration', ''),
            'link': ext.get('link', ''),
            'description': ext.get('description', ''),
            'is_ai': False,
            'is_external': True,
        })

    # ─── Fallback guarantee: if DB is STILL empty, return AI raw ──
    if len(all_courses) == 0 and ai_courses_raw:
        for c in ai_courses_raw:
            if isinstance(c, dict) and c.get('title'):
                all_courses.append({
                    'title': c.get('title', ''),
                    'platform': c.get('platform', ''),
                    'level': c.get('level', level),
                    'level_display': level.capitalize(),
                    'domain': c.get('domain', domain if domain else 'Technology'),
                    'is_free': c.get('is_free', is_free_bool),
                    'learning_type': c.get('learning_type', learning_type),
                    'learning_type_display': learning_type.replace('_', ' ').title(),
                    'duration': c.get('duration', ''),
                    'link': c.get('link', ''),
                    'description': c.get('description', ''),
                    'is_ai': True,
                    'is_external': False,
                })

    # Deduplicate and limit
    final_courses = deduplicate_courses(all_courses)[:15]

    # 🔥 FIX 3: FINAL SAFETY NET — user NEVER sees empty results
    if len(final_courses) == 0:
        final_courses = [
            {
                "title": f"Introduction to {domain or 'Technology'}",
                "platform": "YouTube / Web",
                "level": level,
                "level_display": level.capitalize(),
                "domain": domain or "Technology",
                "is_free": True,
                "learning_type": learning_type,
                "learning_type_display": learning_type.replace('_', ' ').title(),
                "duration": "Varies",
                "link": "https://www.youtube.com/results?search_query=" + (domain or "technology").replace(" ", "+"),
                "description": "Basic learning resources for the selected domain.",
                "is_ai": True,
                "is_external": False,
            }
        ]

    # 🔥 FIX 5: Debug logging for traceability
    print(f"[filter_courses] DB: {len(db_courses)} | External: {len(external_courses_raw)} | AI: {len(ai_courses_raw)} | Final: {len(final_courses)}")

    saved_lookup = _saved_lookup_for_user(request.user)
    course_content_type = ContentType.objects.get_for_model(Course)
    saved_ids = {
        object_id for content_type_id, object_id in saved_lookup
        if content_type_id == course_content_type.id
    }
    response_courses = []
    for course in final_courses:
        item = dict(course)
        item['saved'] = bool(item.get('id') and item['id'] in saved_ids)
        response_courses.append(item)

    # ─── STEP 5: Cache & Return ────────────────────────
    # Only cache if we have results (avoid caching empty)
    if final_courses:
        cache.set(cache_key, final_courses, 600)  # 10 min TTL

    return JsonResponse({
        'success': True,
        'courses': response_courses
    })


# ═════════════ TOOLS & PLATFORMS PAGE ═════════════

def tools(request):
    """Display tools and platforms page."""
    from techbrat.models import Tool
    
    # Get category choices for filter
    categories = [choice[0] for choice in Tool.CATEGORY_CHOICES]
    difficulties = [choice[0] for choice in Tool.DIFFICULTY_CHOICES]
    use_cases = [choice[0] for choice in Tool.USE_CASE_CHOICES]
    
    return render(request, 'tools.html', {
        'categories': categories,
        'difficulties': difficulties,
        'use_cases': use_cases,
        'saved_tool_ids': list(
            SavedItem.objects.filter(
                user=request.user,
                content_type=ContentType.objects.get_for_model(Tool),
            ).values_list('object_id', flat=True)
        ) if request.user.is_authenticated else [],
    })


@require_POST
def filter_tools(request):
    """API endpoint for filtering and searching tools with AI fallback."""
    from techbrat.models import Tool
    from techbrat.tool_fetcher import is_query_tech_related, get_tools_hybrid
    
    data = json.loads(request.body)
    search_query = data.get('search', '').strip()
    category = data.get('category', 'all')
    difficulty = data.get('difficulty', 'all')
    use_case = data.get('use_case', 'all')
    
    # ── TECH VALIDATION ────────────────────────────
    if search_query:
        is_tech, error_msg = is_query_tech_related(search_query)
        if not is_tech:
            return JsonResponse({
                'success': False,
                'message': error_msg or 'This platform supports only technology-related tools and platforms.'
            })
    
    # ── FETCH TOOLS (HYBRID) ───────────────────────
    try:
        tools_data = get_tools_hybrid(
            category=category if category != 'all' else None,
            difficulty=difficulty if difficulty != 'all' else None,
            use_case=use_case if use_case != 'all' else None,
            search_query=search_query if search_query else None
        )
        saved_lookup = _saved_lookup_for_user(request.user)
        serialized_tools = []
        for tool_data in tools_data:
            tool_obj = Tool.objects.filter(name=tool_data.get('name', '')).first()
            if tool_obj:
                serialized_tools.append(_serialize_tool(tool_obj, saved_lookup))
            else:
                item = dict(tool_data)
                item.setdefault('content_type', 'tool')
                item.setdefault('saved', False)
                serialized_tools.append(item)
        
        return JsonResponse({
            'success': True,
            'tools': serialized_tools,
            'count': len(serialized_tools)
        })
    
    except Exception as e:
        print(f"Tools API error: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@require_POST
def tools_recommendations(request):
    """Get AI-generated tool recommendations."""
    from techbrat.tool_fetcher import generate_tools_via_ai, get_tools_hybrid
    
    data = json.loads(request.body)
    category = data.get('category')
    difficulty = data.get('difficulty')
    use_case = data.get('use_case')
    
    try:
        tools = generate_tools_via_ai(
            category=category,
            difficulty=difficulty,
            use_case=use_case,
            count=6
        )
        generated = True
        if not tools:
            tools = get_tools_hybrid(
                category=category,
                difficulty=difficulty,
                use_case=use_case,
                search_query=None
            )
            generated = False
        saved_lookup = _saved_lookup_for_user(request.user)
        serialized_tools = []
        for tool_data in tools:
            tool_obj = Tool.objects.filter(name=tool_data.get('name', '')).first()
            if tool_obj:
                serialized_tools.append(_serialize_tool(tool_obj, saved_lookup))
            else:
                item = dict(tool_data)
                item.setdefault('content_type', 'tool')
                item.setdefault('saved', False)
                serialized_tools.append(item)
        
        return JsonResponse({
            'success': True,
            'tools': serialized_tools,
            'generated': generated
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


# ═════════════ MOTIVATION & TIPS PAGE ═════════════

def motivation(request):
    """Display motivation and tips page."""
    from techbrat.models import Tip
    from techbrat.tip_fetcher import get_daily_tip, get_consistency_streak
    
    # Get category choices for filter
    categories = [choice[0] for choice in Tip.CATEGORY_CHOICES]
    
    # Get daily tip
    daily_tip = get_daily_tip()
    
    # Get consistency streak (for logged-in users)
    streak = 0
    if request.user.is_authenticated:
        streak, _ = get_consistency_streak(request.user)
    
    return render(request, 'motivation.html', {
        'categories': categories,
        'daily_tip': daily_tip,
        'streak': streak,
        'saved_tip_ids': list(
            SavedItem.objects.filter(
                user=request.user,
                content_type=ContentType.objects.get_for_model(Tip),
            ).values_list('object_id', flat=True)
        ) if request.user.is_authenticated else [],
    })


@require_POST
def filter_tips(request):
    """API endpoint for filtering and fetching tips."""
    from techbrat.models import Tip
    from techbrat.tip_fetcher import is_tip_tech_related, get_tips_hybrid
    
    data = json.loads(request.body)
    category = data.get('category', 'all')
    limit = min(int(data.get('limit', 5)), 10)  # Max 10 tips
    
    try:
        tips = get_tips_hybrid(
            category=category if category != 'all' else None,
            limit=limit
        )
        saved_lookup = _saved_lookup_for_user(request.user)
        serialized_tips = []
        for tip_data in tips:
            tip_obj = Tip.objects.filter(title=tip_data.get('title', '')).first()
            if tip_obj:
                serialized_tips.append(_serialize_tip(tip_obj, saved_lookup))
            else:
                item = dict(tip_data)
                item.setdefault('content_type', 'tip')
                item.setdefault('saved', False)
                serialized_tips.append(item)
        
        return JsonResponse({
            'success': True,
            'tips': serialized_tips,
            'count': len(serialized_tips)
        })
    
    except Exception as e:
        print(f"Tips API error: {str(e)}")
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)


@require_POST
def tips_recommendations(request):
    """Get AI-generated motivation tips."""
    from techbrat.tip_fetcher import generate_tips_via_ai, get_tips_hybrid
    
    data = json.loads(request.body)
    category = data.get('category')
    limit = min(int(data.get('limit', 5)), 10)
    
    try:
        tips = generate_tips_via_ai(
            category=category,
            count=limit
        )
        generated = True
        if not tips:
            tips = get_tips_hybrid(
                category=category if category != 'all' else None,
                limit=limit
            )
            generated = False
        saved_lookup = _saved_lookup_for_user(request.user)
        serialized_tips = []
        for tip_data in tips:
            tip_obj = Tip.objects.filter(title=tip_data.get('title', '')).first()
            if tip_obj:
                serialized_tips.append(_serialize_tip(tip_obj, saved_lookup))
            else:
                item = dict(tip_data)
                item.setdefault('content_type', 'tip')
                item.setdefault('saved', False)
                serialized_tips.append(item)
        
        return JsonResponse({
            'success': True,
            'tips': serialized_tips,
            'generated': generated,
            'count': len(serialized_tips)
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)

