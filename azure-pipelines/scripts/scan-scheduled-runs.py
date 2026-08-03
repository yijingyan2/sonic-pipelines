#!/usr/bin/env python3

import argparse
import datetime
import json
import urllib.request
import sys


def parse_timestamp(value):
  return datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))


def normalize_branch(branch):
  if branch.startswith('refs/'):
    return branch
  return 'refs/heads/' + branch


def failed_streaks(runs_by_day, minimum_days):
  failed_days = sorted(
    day for day, results in runs_by_day.items()
    if results and all(result == 'failed' for result in results)
  )
  streaks = []
  current = []
  for day in failed_days:
    if current and day != current[-1] + datetime.timedelta(days=1):
      if len(current) >= minimum_days:
        streaks.append(current)
      current = []
    current.append(day)
  if len(current) >= minimum_days:
    streaks.append(current)
  return streaks


def scan(pipelines, branch_runs, window_start, window_end, minimum_days):
  pipelines_by_id = {
    pipeline['definitionId']: pipeline for pipeline in pipelines
  }
  alerts = []
  missing = []

  for branch, runs in branch_runs:
    normalized_branch = normalize_branch(branch)
    runs_by_pipeline = {definition_id: {} for definition_id in pipelines_by_id}
    for run in runs:
      definition_id = run.get('definition', {}).get('id')
      timestamp = run.get('queueTime') or run.get('startTime')
      run_time = parse_timestamp(timestamp) if timestamp else None
      if (
        definition_id not in pipelines_by_id
        or run.get('reason', '').lower() != 'schedule'
        or run.get('sourceBranch') != normalized_branch
        or not run_time
        or run_time < window_start
        or run_time >= window_end
      ):
        continue
      day = run_time.date()
      runs_by_pipeline[definition_id].setdefault(day, []).append(
        run.get('result', '').lower()
      )

    for definition_id, runs_by_day in runs_by_pipeline.items():
      pipeline = pipelines_by_id[definition_id]
      pipeline_name = pipeline['name']
      if not runs_by_day:
        missing.append((pipeline_name, branch))
        continue
      for streak in failed_streaks(runs_by_day, minimum_days):
        alerts.append((pipeline, branch, streak[0], streak[-1]))

  return alerts, missing


def load_json(path):
  with open(path, encoding='utf-8') as json_file:
    return json.load(json_file)


def send_notification(url, thumbprint, payload):
  request = urllib.request.Request(
    url,
    data=json.dumps(payload).encode('utf-8'),
    headers={'Content-Type': 'application/json', 'thumbprint': thumbprint},
    method='POST'
  )
  with urllib.request.urlopen(request, timeout=30) as response:
    result = json.load(response)
  if not result.get('success'):
    raise RuntimeError(result.get('errmsg', 'Notification request failed'))


def owner_details(owner, excluded_emails=()):
  email = owner.get('ownerEmail', '')
  if not email or email.lower() in {value.lower() for value in excluded_emails}:
    return '', ''
  return owner.get('ownerName') or email, email


def main():
  parser = argparse.ArgumentParser(
    description='Report Azure pipelines with missing or repeatedly failed scheduled runs.'
  )
  parser.add_argument('--pipelines', required=True)
  parser.add_argument(
    '--branch-runs', action='append', nargs=2, metavar=('BRANCH', 'RUNS_JSON'),
    required=True
  )
  parser.add_argument('--lookback-days', type=int, default=7)
  parser.add_argument('--minimum-failed-days', type=int, default=3)
  parser.add_argument('--branch-owners', required=True)
  parser.add_argument('--notification-url', required=True)
  parser.add_argument('--thumbprint', required=True)
  parser.add_argument('--build-url-base', required=True)
  args = parser.parse_args()

  if args.lookback_days < args.minimum_failed_days or args.minimum_failed_days < 1:
    parser.error('lookback days must be at least the positive minimum failed days')

  today = datetime.datetime.now(datetime.timezone.utc).date()
  first_day = today - datetime.timedelta(days=args.lookback_days)
  last_day = today - datetime.timedelta(days=1)
  window_start = datetime.datetime.combine(
    first_day, datetime.time.min, tzinfo=datetime.timezone.utc
  )
  window_end = datetime.datetime.combine(
    today, datetime.time.min, tzinfo=datetime.timezone.utc
  )
  pipelines = load_json(args.pipelines)
  branch_owners = load_json(args.branch_owners)
  branch_runs = [
    (branch, load_json(runs_path)) for branch, runs_path in args.branch_runs
  ]
  alerts, missing = scan(
    pipelines, branch_runs, window_start, window_end, args.minimum_failed_days
  )

  for pipeline_name, branch in missing:
    print(
      '##vso[task.logissue type=warning]No scheduled runs from '
      f'{first_day} through {last_day}: {pipeline_name} ({branch})'
    )
  for pipeline, branch, failure_start, failure_end in alerts:
    pipeline_name = pipeline['name']
    print(
      '##vso[task.logissue type=error]Scheduled runs failed on consecutive '
      f'days from {failure_start} through {failure_end}: '
      f'{pipeline_name} ({branch})'
    )
    branch_owner = (
      {} if normalize_branch(branch) == 'refs/heads/master'
      else branch_owners.get(branch, {})
    )
    pipeline_owner_name, pipeline_owner_email = owner_details(
      pipeline, ('lunyue@microsoft.com', 'yijingyan@microsoft.com')
    )
    branch_owner_name, branch_owner_email = owner_details(
      branch_owner,
      (
        'lunyue@microsoft.com', 'yijingyan@microsoft.com',
        pipeline_owner_email
      )
    )
    payload = {
      'name': 'persistent_pipeline_failure',
      'alert_id': f"{pipeline['definitionId']}-{branch}",
      'pipeline': pipeline_name,
      'branch': branch,
      'pipeline_owner_name': pipeline_owner_name,
      'pipeline_owner_email': pipeline_owner_email,
      'branch_owner_suffix': (
        'for viz' if branch_owner_email else ''
      ),
      'branch_owner_name': branch_owner_name,
      'branch_owner_email': branch_owner_email,
      'url': f"{args.build_url_base}{pipeline['definitionId']}"
    }
    print('Notification payload:')
    print(json.dumps(payload, indent=2, sort_keys=True))
    try:
      send_notification(args.notification_url, args.thumbprint, payload)
    except Exception as error:
      print(
        '##vso[task.logissue type=error]Failed to send notification for '
        f'{pipeline_name} ({branch}): {error}'
      )

  checked = len(pipelines) * len(branch_runs)
  print(
    f'Checked {checked} pipeline/branch combinations: '
    f'{len(alerts)} failure streak(s), {len(missing)} without scheduled runs.'
  )
  return 0


if __name__ == '__main__':
  sys.exit(main())