import json
import os
from datetime import datetime

import pandas as pd
import numpy as np
import requests
from pyotp import TOTP
import logging
from dotenv import load_dotenv

load_dotenv(verbose=True, override=True)

logger = logging.getLogger(__name__)

AUTH_SECRET = os.getenv('PLUTUS_AUTH_SECRET')
USER_ID = os.getenv('PLUTUS_USER_ID')
PASS_ID = os.getenv('PLUTUS_PASS')
SITEKEY = os.getenv('SITEKEY')
CLIENT_ID = os.getenv('CLIENT_ID')
NOTIFICATION_TOKEN = os.getenv('NOTIFICATION_TOKEN')

from common_shared_library.captcha_bypass import CaptchaBypass

class PlutusApi(object):
    def __init__(self, user_id=None, pass_id=None, auth_id=None, client_id=None):
        self.user_field_id = user_id
        self.pass_field_id = pass_id
        self.auth_field_id = auth_id
        self.client_field_id = client_id
        self.base_url = "https://api.plutus.it/"
        self.session = None

    def base_url(self):
        return self.base_url

    def login(self):

        url = "https://authenticate.plutus.it/auth/login"

        g_response = CaptchaBypass(SITEKEY, url).bypass()

        totp = TOTP(self.auth_field_id)
        token = totp.now()

        payload = {
            "email": self.user_field_id,
            "token": token,
            "password": self.pass_field_id,
            "captcha": g_response,
            "client_id": self.client_field_id
        }

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:106.0) Gecko/20100101 Firefox/106.0",
            "Accept": "application/json",
            "Referer": "https://dex.plutus.it/",
            "Content-Type": "application/json",
            "Origin": "https://dex.plutus.it",
        }

        self.session = requests.Session()
        response = self.session.post(url, json=payload, headers=headers)  # login

        # Sometime request will fail because otp token timed out so retry once more

        if 'id_token' not in response.json():
            token = totp.now()

            payload = {
                "email": self.user_field_id,
                "token": token,
                "password": self.pass_field_id,
                "captcha": g_response,
                "client_id": self.client_field_id
            }
            response = self.session.post(url, json=payload, headers=headers)

        headers = {
            "Authorization": "Bearer " + response.json()['id_token'],
            "Connection": "keep-alive",
        }

        self.session.headers.update(headers)

        # return session

    def _get_raw_rewards(self):
        if not self.session:
            self.login()

        response = self.session.get(self.base_url+"platform/transactions/pluton")

        if response.status_code != 200:
            # push_notification(NOTIFICATION_TOKEN, "Plutus Rewards", "Lambda Failed to get transactions 💀")
            return {
                "statusCode": response.status_code,
                "body": {
                    'message': 'failed to get transactions',
                }
            }

        return response.json()

    def get_boosted_rewards(self):
        data = self._get_raw_rewards()
        data = pd.json_normalize(data)
        return data[data["type"] == "BOOST_REWARD"]

    # Rewards
    def get_rewards(self):

        data = self._get_raw_rewards()
        data = pd.json_normalize(data)

        float_values = ['amount', 'rebate_rate', 'base_rate', 'staking_rate', 'contis_transaction.transaction_amount',
                        'fiat_transaction.card_transactions.api_response.TransactionAmount']

        for column in float_values:
            data[column] = data[column].astype(float)

        data['updatedAt'] = pd.to_datetime(data['updatedAt'])
        data['createdAt'] = pd.to_datetime(data['createdAt'])
        data.drop('contis_transaction', axis=1, inplace=True)

        # These are transactions with double rewards voucher
        # "type": "BOOST_REWARD", "reference_type": "pluton_transactions"
        boost_reward_df = data[data["type"] == "BOOST_REWARD"].copy()

        data = data[data["type"] != "BOOST_REWARD"]

        # Drop rows with missing values in specific columns for rows to be checked
        data.dropna(
            subset=['contis_transaction.description', 'fiat_transaction.card_transactions.description'], how='all',
            inplace=True)

        na_condition = data[
            ['contis_transaction.description', 'fiat_transaction.card_transactions.description']].isna().all(axis=1)

        # Create a mask for rows where 'type' is not 'REBATE_BONUS'
        not_rebate_condition = data['type'] != 'REBATE_BONUS'

        # Combine conditions
        condition_to_drop = na_condition & not_rebate_condition

        # Drop rows based on the combined condition
        data_to_check = data[~condition_to_drop]

        data['contis_transaction.description'].fillna(data['fiat_transaction.card_transactions.description'],
                                                      inplace=True)
        data['contis_transaction.transaction_amount'].fillna(
            data['fiat_transaction.card_transactions.api_response.TransactionAmount'].mul(100), inplace=True)
        data['contis_transaction.transaction_amount'] = data['contis_transaction.transaction_amount'].astype(float)

        nas = data[(data['contis_transaction.transaction_amount'].isna()) & (data['type'] != 'REBATE_BONUS')]

        for index, row in nas.iterrows():
            rebate = data[(data["exchange_rate_id"] == row["exchange_rate_id"]) & (
                data["contis_transaction.transaction_amount"].notnull())].head(1).squeeze()
            row['contis_transaction.transaction_amount'] = row[
                'fiat_amount_rewarded']  # Maybe keep as na because perk transaction includes total cost?
            row['contis_transaction.description'] = rebate['contis_transaction.description']
            row['contis_transaction.currency'] = rebate['contis_transaction.currency']

            data.iloc[index] = row

        # data['contis_transaction.transaction_amount'] = data['contis_transaction.transaction_amount'].astype(float)
        # data['contis_transaction.transaction_amount'] = data['contis_transaction.transaction_amount'] / 100

        # plu price at time of transaction in pence
        for index, row in data.iterrows():
            if row['rebate_rate'] == 0.0:
                # fiat_amount_rewarded is 100% of transaction so no need to /100
                data.loc[index, 'plu_price'] = row['fiat_amount_rewarded'] / row['amount']
            else:
                data.loc[index, 'plu_price'] = ((row['contis_transaction.transaction_amount'] / 100) * row[
                    'rebate_rate']) / row['amount']


        def update_descriptions(row):
            original_transaction = data[data["id"] == row["reference_id"]]
            if not original_transaction.empty:
                column_names = ["fiat_transaction.clean_description", "fiat_transaction.card_transactions.description", "plu_price", "fiat_amount_rewarded"]
                for column in column_names:
                    row[column] = original_transaction[column].values[0]
            return row

        boost_reward_df = boost_reward_df.apply(update_descriptions, axis=1)

        # Concatenate the two DataFrames back together
        data = pd.concat([data, boost_reward_df])

        return data

    # Not working
    def get_card_balance(self):

        if not self.session:
            self.login()

        response = self.session.get(
            self.base_url+"platform/consumer/balance")  # {'errors': ['disabled: use cards v3 endpoint']}

        if response.status_code == 200:
            data = json.loads(response.text)
            return float(str(data['AvailableBalance'])[:-2] + '.' + str(data['AvailableBalance'])[-2:])

    def get_transactions(self, limit=300, from_date=None, to_date=None):

        if not self.session:
            self.login()

        url = "https://hasura.plutus.it/v1alpha1/graphql"

        payload = json.dumps({
            "operationName": "transactions_view",
            "variables": {
                "offset": 0,
                "limit": limit,
                "from": from_date,
                "to": to_date
            },
            "query": "query transactions_view($offset: Int, $limit: Int, $from: timestamptz, $to: timestamptz, $type: String) {\n  transactions_view_aggregate(\n    where: {_and: [{date: {_gte: $from}}, {date: {_lte: $to}}]}\n  ) {\n    aggregate {\n      totalCount: count\n      __typename\n    }\n    __typename\n  }\n  transactions_view(\n    order_by: {date: desc}\n    limit: $limit\n    offset: $offset\n    where: {_and: [{date: {_gte: $from}}, {date: {_lte: $to}}, {type: {_eq: $type}}]}\n  ) {\n    id\n    model\n    user_id\n    currency\n    amount\n    date\n    type\n    is_debit\n    description\n    __typename\n  }\n}\n"
        })

        response = self.session.post(url, data=payload)

        return response.json()['data']['transactions_view']

    def get_perks(self):

        response = self.users_perks()
        if response.status_code == 200:
            perks_data = json.loads(response.text)

            # perks_data['total_perks_granted']

            # for dic_ in perks_data['perks']:
            #     print(dic_['label'])
            #     perks.append({'id': dic_['id'], 'perk': dic_['label'], 'percent_complete': dic_["percent_spent"],
            #                   'max_monthly_fiat_reward': dic_["max_mothly_fiat_reward"],
            #                   "available": dic_["available"]})

            return [{'id': dic_['id'], 'perk': dic_['label'], 'percent_complete': dic_["percent_spent"],
                              'max_monthly_fiat_reward': dic_["max_mothly_fiat_reward"],
                              "available": dic_["available"]} for dic_ in perks_data['perks']]

        return []

    def users_perks(self):
        if not self.session:
            self.login()
        return self.session.get(self.base_url+"platform/perks")


    def get_selected_next_month_perks(self):
        response = self.users_perks()
        if response.status_code == 200:
            perks_data = json.loads(response.text)

            # perks_data['total_perks_granted']
            # perks = []
            # for dic_ in perks_data['next_month_perks']:
            #     print(dic_['label'])

                # perks.append(
                #     {'id': dic_['id'], 'perk': dic_['label'], 'max_monthly_fiat_reward': dic_["max_mothly_fiat_reward"],
                #      "available": dic_["available"]})

        # return perks

            return [{'id': dic_['id'], 'perk': dic_['label'], 'max_monthly_fiat_reward': dic_["max_mothly_fiat_reward"],
                     "available": dic_["available"]} for dic_ in perks_data['next_month_perks']]


        return []

    def get_perk_spots_left(self):
        response = self.users_perks()
        if response.status_code == 200:
            return json.loads(response.text)['available']
        return None

    def get_total_perks_granted(self):
        response = self.users_perks()
        if response.status_code == 200:
            perks_data = json.loads(response.text)

        return perks_data['total_perks_granted']

    def perks_api(self):
        if not self.session:
            self.login()

        response = self.session.get(self.base_url+"platform/configurations/perks")
        return json.loads(response.text) if response.status_code == 200 else None

    def get_all_perks(self):
        return [dic_['label'] for dic_ in self.perks_api()['perks']]

    def get_all_perks_with_img(self):
        logger.info(self.perks_api()['perks'])
        return {dic_['label']: dic_['image_url'] for dic_ in self.perks_api()['perks']}


    @staticmethod
    def monthly_count(data):
        # Plu collected per month
        data['amount'] = data['amount'].astype(float)
        valids = data[data['reason'] != "Rejected by admin"]
        valids['createdAt'] = pd.to_datetime(valids['createdAt'])
        per = valids.createdAt.dt.to_period("M")
        g = valids.groupby(per)

        logger.info(f"Monthly count between {valids['createdAt'].min()} and {valids['createdAt'].max()}")
        return g.agg(Sum=('amount', np.sum), plu_mean=('plu_price', np.mean), plu_max=('plu_price', np.max),
                    plu_min=('plu_price', np.min)).round(2)

    @staticmethod
    def get_current_plu_price():
        url = "https://api.coingecko.com/api/v3/simple/price/?ids=pluton&vs_currencies=gbp"

        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/116.0',
            'Accept': '*/*',
            'Referer': 'https://beincrypto.com/',
            'Content-Type': 'application/json',
            'Origin': 'https://beincrypto.com',
            'Connection': 'keep-alive',
        }

        response = requests.request("GET", url, headers=headers)
        return response.json()['pluton']['gbp']


if __name__ == '__main__':
    AUTH_SECRET = os.getenv('PLUTUS_AUTH_SECRET')
    USER_ID = os.getenv('PLUTUS_USER_ID')
    PASS_ID = os.getenv('PLUTUS_PASS')
    SITEKEY = os.getenv('SITEKEY')
    CLIENT_ID = os.getenv('CLIENT_ID')
    NOTIFICATION_TOKEN = os.getenv('NOTIFICATION_TOKEN')

    api = PlutusApi(USER_ID, PASS_ID, AUTH_SECRET, CLIENT_ID)
    # api.login()

    data = api.get_rewards()

    BOOST_REWARD_df = data[data["type"] == "BOOST_REWARD"].copy()

    ace = api.get_boosted_rewards()
    ace['createdAt'] = pd.to_datetime(ace['createdAt'])

    # Get the current date and time
    now = datetime.now()

    # Check if there are any rewards in the current month using vectorized operations
    if ((ace['createdAt'].dt.year == now.year) & (ace['createdAt'].dt.month == now.month)).any():
        print("Yes")
    else:
        print("No")


    # session = api.login()

#     # url = "https://hasura.plutus.it/v1alpha1/graphql"

#     # payload = "{\"operationName\":\"getBalance\",\"variables\":{\"currency\":\"GBP\"},\"query\":\"query getBalance($currency: enum_fiat_balance_currency!) {\\n  fiat_balance(where: {currency: {_eq: $currency}}) {\\n    id\\n    user_id\\n    currency\\n    amount\\n    created_at\\n    updated_at\\n    __typename\\n  }\\n  card_transactions_aggregate(\\n    where: {type: {_eq: \\\"AUTHORISATION\\\"}, status: {_eq: \\\"APPROVED\\\"}}\\n  ) {\\n    aggregate {\\n      sum {\\n        billing_amount\\n        __typename\\n      }\\n      __typename\\n    }\\n    __typename\\n  }\\n}\\n\"}"

#     # headers = {
#     #     "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/118.0",
#     #     "Accept": "*/*",
#     #     "Accept-Language": "en-GB,en;q=0.5",
#     #     "Accept-Encoding": "gzip, deflate, br",
#     #     "Referer": "https://dex.plutus.it/",
#     #     "content-type": "application/json",
#     #     "Authorization": "Bearer ...",
#     #     "Origin": "https://dex.plutus.it",
#     #     "Connection": "keep-alive",
#     #     "Sec-Fetch-Dest": "empty",
#     #     "Sec-Fetch-Mode": "cors",
#     #     "Sec-Fetch-Site": "same-site",
#     #     "TE": "trailers"
#     # }

#     # response = requests.request("POST", url, json=payload, headers=headers)

#     # print(response.text)

#     rewards = api.get_rewards()

#     # count = monthly_count(transactions)

#     valids = rewards[rewards['reason'] != "Rejected by admin"]
#     valids['createdAt'] = pd.to_datetime(valids['createdAt'])
#     per = valids.createdAt.dt.to_period("M")
#     g = valids.groupby(per)
#     # print(g.sum()['amount']) # stopped working TypeError: datetime64 type does not support sum operations
#     print(g['amount'].sum())  # new syntax

#     transactions = api.get_transactions()
#     txn = pd.DataFrame(transactions)

#     perks = api.get_all_perks()
