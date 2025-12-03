from pytradekit.restful.binance_restful import BinanceClient
from pytradekit.utils.time_handler import get_millisecond_str, get_datetime, convert_timestamp_to_datetime
from pytradekit.utils.static_types import DepositWithdraw
from pytradekit.utils.dynamic_types import BinanceRestful, DepositWithdrawAuxiliary


class HandleRestfulDepositWithdraw:

    def get_deposit(self, bn_cli, account_id, run_time_ms):
        deposit = bn_cli.get_deposit_history()
        des = []
        if deposit is None:
            return des
        for i in deposit:
            status = i[BinanceRestful.state.value]
            if status == 1:
                state = DepositWithdrawAuxiliary.deposit_comformed.value
            else:
                continue
            event_time_ms = get_millisecond_str(get_datetime())
            time_ms1 = i[BinanceRestful.deposit_time.value]
            time_ms = get_millisecond_str(convert_timestamp_to_datetime(time_ms1))
            deposit_id = i[BinanceRestful.id.value]
            coin = i[BinanceRestful.coin.value]
            quantity = float(i[BinanceRestful.quantity.value])
            txid = i[BinanceRestful.txid.value]
            fee = None
            network = None
            is_inner = {}
            other = {}
            data = DepositWithdraw(event_time_ms=event_time_ms,
                                   run_time_ms=run_time_ms,
                                   time_ms=time_ms,
                                   account_id=account_id,
                                   id=deposit_id,
                                   coin=coin,
                                   is_inner=is_inner,
                                   txid=txid,
                                   quantity=quantity, fee=fee,
                                   network=network,
                                   state=state, other=other)
            des.append(data.to_dict())

        return des

    def get_withdraw(self, bn_cli, account_id, run_time_ms):
        withdraw = bn_cli.get_withdraw_history()
        withd = []
        if withdraw is None:
            return withd
        for i in withdraw:
            status = i[BinanceRestful.state.value]
            if status == 6:
                state = DepositWithdrawAuxiliary.deposit_comformed.value
            else:
                continue
            event_time_ms = get_millisecond_str(get_datetime())
            time_ms3 = i[BinanceRestful.withdraw_time.value]
            time_ms = time_ms3 + '.000'
            withdraw_id = i[BinanceRestful.id.value]
            coin = i[BinanceRestful.coin.value]
            quantity = -float(i[BinanceRestful.quantity.value])
            fee = i[BinanceRestful.withdraw_fee.value]
            network = i[BinanceRestful.network.value]
            is_inner = {}
            txid = i[BinanceRestful.txid.value]
            other = {}
            data = DepositWithdraw(event_time_ms=event_time_ms,
                                   run_time_ms=run_time_ms,
                                   time_ms=time_ms,
                                   account_id=account_id,
                                   id=withdraw_id,
                                   coin=coin,
                                   is_inner=is_inner,
                                   txid=txid,
                                   quantity=quantity,
                                   fee=fee,
                                   network=network,
                                   state=state, other=other)
            withd.append(data.to_dict())

        return withd

    def get_transfer(self, bn_cli, account_id, run_time_ms):
        transfer = bn_cli.get_transfer_history()
        tran = []
        if transfer is None or 'code' in transfer:
            pass
        else:
            for i in transfer:
                event_time_ms = get_millisecond_str(get_datetime())
                time_ms2 = i[BinanceRestful.transfer_time.value]
                time_ms = get_millisecond_str(convert_timestamp_to_datetime(time_ms2))
                transfer_id = i[BinanceRestful.transfer_id.value]
                coin = i[BinanceRestful.transfer_coin.value]
                quantity = float(i[BinanceRestful.transfer_quantity.value])
                status = i[BinanceRestful.state.value]
                if status == BinanceRestful.transfer_state_comformed.value:
                    state = DepositWithdrawAuxiliary.deposit_comformed.value
                else:
                    state = DepositWithdrawAuxiliary.deposit_comforming.value
                is_inner = {BinanceRestful.transfer_from.value: i[BinanceRestful.transfer_from.value],
                            BinanceRestful.transfer_to.value: i[BinanceRestful.transfer_to.value]}
                fee = None
                network = None
                txid = None
                other = {}
                data = DepositWithdraw(event_time_ms=event_time_ms,
                                       run_time_ms=run_time_ms,
                                       time_ms=time_ms,
                                       account_id=account_id,
                                       id=transfer_id,
                                       coin=coin,
                                       is_inner=is_inner,
                                       txid=txid,
                                       quantity=quantity,
                                       fee=fee,
                                       network=network,
                                       state=state, other=other)
                tran.append(data.to_dict())
        return tran

    def get_transfer_sub(self, bn_cli, account_id, run_time_ms):
        transfer = bn_cli.get_transfer_history_sub()
        tran = []
        if transfer is None or 'code' in transfer:
            pass
        else:
            for i in transfer:
                event_time_ms = get_millisecond_str(get_datetime())
                time_ms2 = i[BinanceRestful.transfer_time.value]
                time_ms = get_millisecond_str(convert_timestamp_to_datetime(time_ms2))
                transfer_id = i[BinanceRestful.transfer_id.value]
                coin = i[BinanceRestful.transfer_coin.value]
                quantity = float(i[BinanceRestful.transfer_quantity.value])
                quantity = quantity if i[BinanceRestful.transfer_type.value] == 1 else -quantity
                status = i[BinanceRestful.state.value]
                if status == BinanceRestful.transfer_state_comformed.value:
                    state = DepositWithdrawAuxiliary.deposit_comformed.value
                else:
                    state = DepositWithdrawAuxiliary.deposit_comforming.value
                is_inner = {BinanceRestful.transfer_from.value: i[BinanceRestful.transfer_email.value]}
                fee = None
                network = None
                txid = None
                other = {}
                data = DepositWithdraw(event_time_ms=event_time_ms,
                                       run_time_ms=run_time_ms,
                                       time_ms=time_ms,
                                       account_id=account_id,
                                       id=transfer_id,
                                       coin=coin,
                                       is_inner=is_inner,
                                       txid=txid,
                                       quantity=quantity,
                                       fee=fee,
                                       network=network,
                                       state=state, other=other)
                tran.append(data.to_dict())
        return tran

    def run(self, logger, api_key, api_secret, account_id, run_time_ms):
        bn_cli = BinanceClient(logger, key=api_key, secret=api_secret)
        des = self.get_deposit(bn_cli, account_id, run_time_ms)
        withd = self.get_withdraw(bn_cli, account_id, run_time_ms)
        tran = self.get_transfer(bn_cli, account_id, run_time_ms)
        tran_sub = self.get_transfer_sub(bn_cli, account_id, run_time_ms)
        deposit_withdraw = des + withd + tran + tran_sub
        return deposit_withdraw
