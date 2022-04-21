import logging
from typing import List
import datetime
from enum import Enum
import asyncio

from aioredis import Redis
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi_plugins import depends_redis
from fastapi_limiter.depends import RateLimiter
from prometheus_client.metrics import Counter, Histogram
import pytz

from api.api_v1.models import (
    AggregateRewards,
    InitialBalance,
    EndOfDayBalance,
    ValidatorRewards,
)
from providers.beacon_node import depends_beacon_node, BeaconNode, GENESIS_DATETIME
from providers.coin_gecko import depends_coin_gecko, CoinGecko
from providers.db_provider import depends_db, DbProvider

# Beaconcha.in graft
# import urllib library
from urllib.request import urlopen

# import json
import json
from types import SimpleNamespace

router = APIRouter()
logger = logging.getLogger(__name__)


def applyRPRewards(reward, commission):
    return reward * (1 + commission) / 2


TimezoneEnum = Enum(
    "TimezoneEnum",
    {timezone.replace("/", ""): timezone for timezone in pytz.common_timezones},
)

REWARDS_REQUEST_COUNT = Counter(
    "rewards_request",
    "Amount of times rewards were requested",
    labelnames=("timezone", "currency", "calendar_year"),
)
VALIDATORS_PER_REWARDS_REQUEST = Histogram(
    "validators_per_rewards_request",
    "Amount of validator indexes contained in a /rewards request",
    buckets=[0, 1, 2, 3, 4, 5, 10, 20, 50, 100, 1000, 10000, float("inf")],
)


@router.get(
    "/rewards",
    response_model=AggregateRewards,
    summary="Returns per-day rewards for the specified validator indexes.",
)
async def rewards(
    validator_indexes: List[int] = Query(
        ...,
        description="The validator indexes for which to retrieve rewards.",
        min_items=1,
        example=[1, 2, 3],
    ),
    start_date: datetime.date = Query(
        ...,
        description="The date at which to start.",
        example=datetime.date.fromisoformat("2020-01-01"),
    ),
    end_date: datetime.date = Query(
        ...,
        description="The date at which to stop.",
        example=datetime.date.fromisoformat("2020-12-31"),
    ),
    timezone: TimezoneEnum = Query(
        ...,
        description="The timezone for which to calculate rewards.",
        example=TimezoneEnum.EuropeAmsterdam,
    ),
    currency: str = Query(
        ...,
        description="One of the currencies supported by CoinGecko - one of "
        '<a href="https://api.coingecko.com/api/v3/simple/supported_vs_currencies">these</a>.',
        min_length=3,
        max_length=4,
        example="EUR",
    ),
    cache: Redis = Depends(depends_redis),
    db_provider: DbProvider = Depends(depends_db),
    beacon_node: BeaconNode = Depends(depends_beacon_node),
    coin_gecko: CoinGecko = Depends(depends_coin_gecko),
    rate_limiter: RateLimiter = Depends(RateLimiter(times=1, seconds=1)),
):
    validator_indexes = set(validator_indexes)
    logger.info(
        f"Getting rewards for date range {start_date} - {end_date} "
        f"for {len(validator_indexes)} validators"
    )
    VALIDATORS_PER_REWARDS_REQUEST.observe(len(validator_indexes))

    # See if rewards for a calendar year were requested
    cal_year_cond = (
        start_date.day == 1
        and start_date.month == 1
        and end_date.day == 31
        and end_date.month == 12
        and start_date.year == end_date.year
    )

    # Split the REWARDS_REQUEST_COUNT metrics by timezone, daterange and currency
    today = datetime.date.today()
    ytd_cond = (
        start_date.day == 1
        and start_date.month == 1
        and start_date.year == today.year
        and end_date == today
    )
    since_genesis_cond = start_date <= GENESIS_DATETIME.date() and end_date == today
    if cal_year_cond:
        calendar_year = start_date.year
    elif ytd_cond:
        calendar_year = "YTD"
    elif since_genesis_cond:
        calendar_year = "since genesis"
    else:
        calendar_year = "other"

    REWARDS_REQUEST_COUNT.labels(timezone.value, currency, calendar_year).inc()

    # Increment the end date by 1 day to make it inclusive
    end_date = end_date + datetime.timedelta(days=1)

    # Create timezone object and create a start datetime at 00:00 of that day
    timezone = pytz.timezone(timezone.value)
    start_dt = datetime.datetime.combine(start_date, datetime.time.min)

    # Parse the currency
    currency = currency.upper()
    supported_currencies = await coin_gecko.supported_vs_currencies(cache)
    if currency not in supported_currencies:
        raise HTTPException(
            status_code=400, detail=f"Unsupported currency - {currency}"
        )

    # Cap the start date at genesis
    start_dt_utc = max(
        timezone.localize(start_dt).astimezone(pytz.utc), GENESIS_DATETIME
    )

    # To retrieve the validator balances, we need to know
    # which slots to retrieve them for
    slots_needed = set()

    # We will need to get the initial balance for the requested time period
    # This "initial" slot could be different for each validator, which is why
    # we don't just add the activation_slots to slots_needed
    first_slot_in_requested_period = await BeaconNode.slot_for_datetime(start_dt_utc)
    activation_slots = await beacon_node.activation_slots_for_validators(
        validator_indexes, cache
    )
    initial_balances = {}
    for activation_slot in set(activation_slots.values()):
        if activation_slot is None:
            continue
        vi_with_this_as = []
        for vi in validator_indexes:
            if activation_slots[vi] == activation_slot:
                vi_with_this_as.append(vi)

        # In case the validator is being activated during the requested time period,
        # the initial balance will be equal to its balance in the activation epoch.
        if activation_slot > first_slot_in_requested_period:
            initial_balance_slot = activation_slot
            initial_balances_tmp = await beacon_node.balances_for_slot(
                initial_balance_slot, vi_with_this_as
            )
        else:
            initial_balance_slot = await BeaconNode.slot_for_datetime(start_dt_utc)
            initial_balances_tmp = db_provider.balances(
                slots=[initial_balance_slot], validator_indexes=vi_with_this_as
            )

        for vi in vi_with_this_as:
            balance = next(
                ib for ib in initial_balances_tmp if ib.validator_index == vi
            )
            initial_balances[vi] = InitialBalance(
                date=(
                    await BeaconNode.datetime_for_slot(initial_balance_slot, timezone)
                ).date(),
                slot=initial_balance_slot,
                balance=balance.balance,
            )

    # - We'll also need balances at midnight for each day in the requested date range
    #   I'm using 23:59:59, 1 second before midnight here, for convenience reasons
    #   it's easier to understand the concept of end-of-day balance for a day than
    #   to think in start-of-day balance for the next day )
    #   I'm working with dates here (instead of datetime objects) because it's easier to
    #   work around DST issues if you localize a datetime just-in-time - as late as
    #   possible ( datetime arithmetics with DST doesn't work the way you'd expect )
    current_date = start_dt_utc.astimezone(timezone).date()
    eod_slots = set()
    while current_date < end_date:
        # Create a midnight datetime object
        current_dt = datetime.datetime.combine(
            current_date, time=datetime.time(hour=23, minute=59, second=59)
        )
        # Localize it
        current_dt = timezone.localize(current_dt)
        # Retrieve the midnight slot number
        eod_slots.add(await BeaconNode.slot_for_datetime(current_dt))
        current_date += datetime.timedelta(days=1)
    slots_needed.update(eod_slots)

    # And lastly, if the end_date is today/in the future,
    # the user probably wants to see today's rewards too,
    # not just up til the last end-of-day slot, so include
    # the head slot
    head_slot = await BeaconNode.head_slot()
    if any([s > head_slot for s in slots_needed]):
        logger.debug("Including head slot!")
        slots_needed.add(head_slot)

    # Keep only slots that are <= head slot
    # since we cannot retrieve balances for future
    # slots
    slots_needed = [s for s in slots_needed if s <= head_slot]
    slots_needed = sorted(slots_needed)

    logger.debug(f"Slots: {slots_needed}")

    # Retrieve the balances for the needed slots from the database
    logger.debug("Retrieving balances from DB")
    balances = db_provider.balances(
        slots=slots_needed, validator_indexes=validator_indexes
    )

    # Balances for the head slot are not present in the database
    # - these need to be retrieved on-demand from the beacon node
    logger.debug("Retrieving head slot balances from beacon node")
    if head_slot in slots_needed:
        balances.extend(
            await beacon_node.balances_for_slot(head_slot, validator_indexes)
        )

    # Retrieve ETH price for the relevant dates
    # - these are determined by the end-of-day slots
    #   and the head slot (if present) - so basically all slots
    #   in slots_needed except for the first slot in there (the
    #   first slot on start_date)
    logger.debug("Retrieving ETH prices")
    # Use a semaphore to make sure we don't overload CoinGecko with
    # too many requests at once in case the prices aren't cached
    sem = asyncio.Semaphore(5)
    date_eth_price = {}
    for slot in slots_needed:
        date = (await BeaconNode.datetime_for_slot(slot, timezone)).date()

        async with sem:
            date_eth_price[date] = await coin_gecko.price_for_date(
                date, currency, cache
            )

    # Order the prices nicely
    date_eth_price = {date: price for date, price in sorted(date_eth_price.items())}
    logger.debug(date_eth_price)

    # Populate the object to return
    logger.debug("Populating object to return")

    aggregate_rewards = AggregateRewards(
        validator_rewards=[],
        currency=currency,
        eth_prices=date_eth_price,
    )
    for validator_index in sorted(validator_indexes):
        # Skip pending validators
        if activation_slots[validator_index] is None:
            continue
        validator_balances = [
            b for b in balances if b.validator_index == validator_index
        ]
        # get the commission rate from beaconchain, 1 if it's not RP

        # Sort them by the slot number
        validator_balances = sorted(validator_balances, key=lambda x: x.slot)

        # Populate the initial and end-of-day balances
        initial_balance = initial_balances[validator_index]
        prev_balance = initial_balance.balance
        eod_balances = []
        total_eth = 0
        total_currency = 0
        rp_commission = 0.15
        url = f"https://beaconcha.in/api/v1/rocketpool/validator/{validator_index}"
        # store the response of URL
        json_resp = urlopen(url)
        # storing the JSON response
        # from url in data
        response = json.loads(json_resp, object_hook=lambda d: SimpleNamespace(**d))
        print(response)
        if response.status != 200:
            raise Exception(
                f"Error while fetching data from beaconcha.in: {response.content.decode()}"
            )
        else:
            rp_commission = response.data["minipool_node_fee"]
            print(rp_commission)
        # fix initial balance
        if rp_commission != 1:
            prev_balance = applyRPRewards(prev_balance, rp_commission)

        for vb in validator_balances:
            vb.balance = applyRPRewards(vb.balance, rp_commission)
            slot_date = (await BeaconNode.datetime_for_slot(vb.slot, timezone)).date()

            eod_balances.append(
                EndOfDayBalance(date=slot_date, slot=vb.slot, balance=vb.balance)
            )

            # Calculate earnings
            day_rewards_eth = vb.balance - prev_balance
            total_eth += day_rewards_eth
            total_currency += day_rewards_eth * date_eth_price[slot_date]

            # Set prev_balance to current balance for next loop iteration
            prev_balance = vb.balance

        aggregate_rewards.validator_rewards.append(
            ValidatorRewards(
                validator_index=validator_index,
                initial_balance=initial_balance,
                eod_balances=eod_balances,
                total_eth=total_eth,
                total_currency=total_currency,
                rp_commission=rp_commission,
            )
        )
    return aggregate_rewards
