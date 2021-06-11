import time
from typing import List, Dict

from blspy import G1Element, AugSchemeMPL

from lib.std.types.coin import Coin
from lib.std.types.ints import uint32, uint64
from lib.std.types.sized_bytes import bytes32
from lib.std.types.spend_bundle import SpendBundle
from lib.std.types.coin_record import CoinRecord
from lib.std.types.streamable import dataclass_from_dict
from lib.std.types.mempool_inclusion_status import MempoolInclusionStatus
from lib.std.types.full_block import FullBlock
from lib.std.sim.coinbase import create_pool_coin, create_farmer_coin, create_puzzlehash_for_pk
from lib.std.sim.block_rewards import calculate_pool_reward, calculate_base_farmer_reward
from lib.std.sim.spend_bundle_validation import validate_spendbundle
from lib.std.sim.bundle_tools import simple_solution_generator
from lib.std.sim.default_constants import DEFAULT_CONSTANTS
from lib.std.util.timestamp import float_to_timestamp

class Node():
    block_height: uint32 = 0
    timestamp: uint64 = 0
    blocks: List[FullBlock] = []
    coins: List[Coin] = []
    coin_records: List[CoinRecord] = []
    mempool: List[SpendBundle] = []

    def __init__(self):
        self.timestamp = float_to_timestamp(time.time())

    def set_block_height(self, block_height: uint32):
        self.block_height = block_height

    def set_timestamp(self, timestamp: uint64):
        self.timestamp = timestamp

    def add_coin(self, coin: Coin):
        #Add the coin to the UTXO set
        self.coins.append(coin)
        #Create a coin record for it
        record = CoinRecord(
            coin,
            self.block_height,
            0,
            False,
            False,
            self.timestamp
        )
        self.coin_records.append(record)

    def remove_coin(self, coin: Coin):
        #Remove the coin from the UTXO set
        old_len = len(self.coins)
        self.coins = list(filter(lambda x: x.name() != coin.name(), self.coins))
        assert len(self.coins) == old_len - 1

        #Update the coin record
        matching_record = list(filter(lambda e: e.coin == coin,self.coin_records))
        for record in matching_record:
            updated_record = CoinRecord(
                coin,
                record.confirmed_block_index,
                self.block_height,
                True,
                record.coinbase,
                self.timestamp
            )
            self.coin_records.remove(record)
            self.coin_records.append(updated_record)

    def get_coins(self, coin_filter={}):
        filtered_coins = self.coins
        for key in coin_filter:
            filtered_coins = list(filter(lambda e: e.__dict__[key] == coin_filter[key], filtered_coins))
        return filtered_coins

    def get_coin_record_by_coin_name(self, name):
        coin_record = list(filter(lambda e: e.coin.name() == name, self.coin_records))
        if len(coin_record) > 0:
            return coin_record[0]
        else:
            return None

    def generate_transaction_generator(self):
        if len(self.mempool) == 0:
            return None

        signatures = []
        coin_solutions = []
        for bundle in self.mempool:
            signatures.append(bundle.aggregated_signature)
            coin_solutions.extend(bundle.coin_solutions)

        agg_sig = AugSchemeMPL.aggregate(signatures)
        total_bundle = SpendBundle(
            coin_solutions,
            agg_sig
        )

        return simple_solution_generator(total_bundle)

    def farm_block(self, public_key: G1Element):
        # Fees get calculated
        fees = 0
        for bundle in self.mempool:
            fees += bundle.fees()

        # Coins get moved
        removals = []
        additions = []
        for bundle in self.mempool:
            for removal in bundle.removals():
                removals.append(removal)
            for addition in bundle.additions():
                additions.append(addition)

        for removal in removals:
            self.remove_coin(removal)
        for addition in additions:
            self.add_coin(addition)

        # Rewards get generated
        pool_coin = create_pool_coin(
            self.block_height,
            create_puzzlehash_for_pk(public_key),
            calculate_pool_reward(self.block_height),
            DEFAULT_CONSTANTS.GENESIS_CHALLENGE
        )
        farmer_coin = create_farmer_coin(
            self.block_height,
            create_puzzlehash_for_pk(public_key),
            (calculate_base_farmer_reward(self.block_height) + fees),
            DEFAULT_CONSTANTS.GENESIS_CHALLENGE
        )
        self.add_coin(pool_coin)
        self.add_coin(farmer_coin)

        #Block is created
        self.blocks.append(FullBlock(
            [pool_coin, farmer_coin],
            self.generate_transaction_generator(),
            self.block_height,
        ))

        # mempool is cleared
        self.mempool = []

        # block_height is incremented
        self.block_height += 1

        # timestamp is reset
        self.timestamp = float_to_timestamp(time.time())
        return {
            'additions': [pool_coin, farmer_coin] + additions,
            'removals': removals
        }

    def push_tx(self, spend_bundle: SpendBundle):
        spend_name = spend_bundle.name()

        removals = []
        for bundle in self.mempool:
            for removal in bundle.removals():
                removals.append(removal)

        if spend_bundle in self.mempool:
            status = MempoolInclusionStatus.SUCCESS
            error = None
        else:
            cost, status, error = validate_spendbundle(spend_bundle, removals, self.coin_records, self.block_height)
            if status != MempoolInclusionStatus.SUCCESS:
                if spend_bundle in self.mempool:
                    # Already in mempool
                    status = MempoolInclusionStatus.SUCCESS
                    error = None

        if status == MempoolInclusionStatus.FAILED:
            assert error is not None
            raise ValueError(f"Failed to include transaction {spend_name}, error {error.name}")

        if status == MempoolInclusionStatus.SUCCESS:
            self.mempool.append(spend_bundle)

        return {
            "status": status.name,
            "cost": cost
        }
