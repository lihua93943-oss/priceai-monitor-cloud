"""Fetch a public reference price and report it to the private cloud monitor."""
import json
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.request import Request, urlopen

TARGET = 'https://priceai.cc/products/chatgpt-plus'


class DescriptionParser(HTMLParser):
    description = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag.lower() == 'meta' and attributes.get('name', '').lower() == 'description':
            self.description = attributes.get('content', '')


def main():
    print('Job reached Python at', datetime.now(timezone.utc).isoformat(), flush=True)
    if os.environ.get('SCHEDULER_ONLY') == 'true':
        print('Timing probe completed without accessing the price website.')
        return
    base = os.environ['WORKER_URL'].rstrip('/')
    if not re.fullmatch(r'https://[a-z0-9-]+\.[a-z0-9-]+\.workers\.dev', base):
        raise ValueError('Unexpected monitor URL')

    def report(path, data):
        request = Request(base + path, data=json.dumps(data).encode(), headers={
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + os.environ['REPORT_TOKEN'],
        })
        with urlopen(request, timeout=45) as response:
            return json.load(response)

    with urlopen(base + '/health', timeout=20) as response:
        health = json.load(response)
    if (health.get('source_backoff') or {}).get('next_allowed_at', 0) > time.time() * 1000:
        print('Source cooldown active. No price request sent.')
        return
    dry_run = os.environ.get('DRY_RUN') == 'true'
    if not health.get('enabled') and not dry_run:
        print('Monitor paused.')
        return
    try:
        with urlopen(Request(TARGET, headers={'User-Agent': 'Mozilla/5.0'}), timeout=30) as response:
            html = response.read(262144).decode('utf-8', errors='replace')
    except HTTPError as error:
        if error.code != 429:
            raise
        retry = error.headers.get('Retry-After', '3600')
        try:
            seconds = int(retry)
        except ValueError:
            try:
                seconds = int(parsedate_to_datetime(retry).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                seconds = 3600
        # Never retry in this job. The cloud scheduler also observes this cooldown.
        seconds = max(60, min(86400, seconds))
        print(json.dumps(report('/source-error', {'status': 429, 'retry_after_seconds': seconds})))
        return
    parser = DescriptionParser()
    parser.feed(re.split(r'</head\s*>', html, maxsplit=1, flags=re.I)[0])
    match = re.search(r'当前参考\s*[:：]\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)(?![\d.])', parser.description or '')
    if not match:
        raise ValueError('Reference price missing from meta description')
    cents = int(Decimal(match.group(1)) * 100)
    if not 0 < cents <= 10000000:
        raise ValueError('Invalid price')
    print(f'Reference price CNY {cents / 100:.2f}', flush=True)
    if dry_run:
        print('Dry run completed without changing price state or sending email.')
        return
    result = report('/ingest', {'price_cents': cents, 'observed_at': int(time.time() * 1000)})
    print(json.dumps(result, ensure_ascii=False))
    if result.get('outcome') not in {'ok', 'paused', 'stale'}:
        raise RuntimeError('Price report was not processed')


if __name__ == '__main__':
    main()
