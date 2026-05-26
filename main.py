#!/usr/bin/env python3
"""
Cybersecurity Research Agent + Excel Generator

Main entry point for the TUI-driven, human-in-the-loop research pipeline.
"""

import asyncio
import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from argparse import Namespace

from config.settings import settings
from db.store import init_db, get_domain_id, get_subdomains, upsert_subdomain


def setup_logging(tui_mode: bool = False) -> str:
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    
    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"session_{session_id}.log"
    
    file_handler = logging.FileHandler(log_file, mode='w', encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
        datefmt='%H:%M:%S'
    ))
    
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    
    if not tui_mode:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(getattr(logging, settings.log_level_console))
        console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root_logger.addHandler(console_handler)
    
    logging.info("=" * 60)
    logging.info(f"SESSION START: {session_id}")
    logging.info("=" * 60)
    
    return str(log_file)


async def run_discovery(domain: str) -> None:
    from agents.discovery import discover_subdomains
    
    logging.info(f"Starting discovery for domain: {domain}")
    subdomains = await discover_subdomains(domain)
    print(f"\nDiscovered {len(subdomains)} subdomains for {domain}:")
    for sd in subdomains:
        print(f"  - {sd['name']}")


async def run_single_subdomain(domain: str, subdomain_name: str) -> None:
    from orchestrator.graph import run_worker_pipeline
    
    domain_id = await get_domain_id(domain)
    if not domain_id:
        print(f"Error: Domain '{domain}' not found")
        return
    
    subdomain_id = await upsert_subdomain(domain_id, subdomain_name)
    
    local_event_queue: asyncio.Queue = asyncio.Queue()
    
    async def print_events():
        while True:
            try:
                event = await asyncio.wait_for(local_event_queue.get(), timeout=1.0)
                print(f"[{event.step}] {event.message} ({event.progress_pct*100:.0f}%)")
                if event.event_type in ("completed", "failed"):
                    break
            except asyncio.TimeoutError:
                continue
    
    event_task = asyncio.create_task(print_events())
    
    try:
        await run_worker_pipeline(domain, subdomain_id, subdomain_name, local_event_queue)
    except Exception as e:
        logging.error(f"Pipeline failed: {e}")
    finally:
        event_task.cancel()
        try:
            await asyncio.wait_for(event_task, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


async def run_tui_mode(log_file: str) -> None:
    from tui.app import run_tui
    await run_tui(log_file)


async def run_batch_mode(domains: list[str] | None = None) -> None:
    from agents.discovery import discover_subdomains
    from orchestrator.graph import run_multiple_workers
    
    if domains is None:
        domains = []
    
    all_tasks = []
    
    for domain in domains:
        logging.info(f"Processing domain: {domain}")
        subdomains = await discover_subdomains(domain)
        
        for sd in subdomains:
            all_tasks.append((domain, sd["id"], sd["name"]))
    
    if not all_tasks:
        print("No subdomains to process")
        return
    
    print(f"Starting batch processing for {len(all_tasks)} subdomains...")
    
    local_event_queue: asyncio.Queue = asyncio.Queue()
    
    async def print_events():
        while True:
            try:
                event = await asyncio.wait_for(local_event_queue.get(), timeout=1.0)
                print(f"[{event.step}] {event.subdomain}: {event.message}")
            except asyncio.TimeoutError:
                continue
    
    event_task = asyncio.create_task(print_events())
    
    try:
        results = await run_multiple_workers(all_tasks, local_event_queue)
        success = sum(1 for r in results if not isinstance(r, Exception))
        print(f"\nBatch complete: {success}/{len(results)} successful")
    finally:
        event_task.cancel()
        try:
            await asyncio.wait_for(event_task, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cybersecurity Research Agent + Excel Generator"
    )
    parser.add_argument(
        "--mode",
        choices=["tui", "discover", "single", "batch"],
        default="tui",
        help="Run mode: tui (interactive), discover, single, or batch"
    )
    parser.add_argument(
        "--domain",
        type=str,
        help="Domain to process (for discover/single mode)"
    )
    parser.add_argument(
        "--subdomain",
        type=str,
        help="Subdomain name (for single mode)"
    )
    parser.add_argument(
        "--domains",
        type=str,
        nargs="+",
        help="Domains to process (for batch mode)"
    )
    
    args = parser.parse_args()
    
    log_file = setup_logging(tui_mode=(args.mode == "tui"))
    
    asyncio.run(_async_main(args, log_file))


async def _async_main(args: argparse.Namespace, log_file: str) -> None:
    from db.store import shutdown_db
    
    try:
        await init_db()
        
        if args.mode == "tui":
            await run_tui_mode(log_file)
        
        elif args.mode == "discover":
            if not args.domain:
                print("Error: --domain required for discover mode")
                return
            await run_discovery(args.domain)
        
        elif args.mode == "single":
            if not args.domain or not args.subdomain:
                print("Error: --domain and --subdomain required for single mode")
                return
            await run_single_subdomain(args.domain, args.subdomain)
        
        elif args.mode == "batch":
            await run_batch_mode(args.domains)
    finally:
        logging.info("=" * 60)
        logging.info("SESSION END")
        logging.info("=" * 60)
        await shutdown_db()


if __name__ == "__main__":
    main()
