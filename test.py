import requests
import json
from requests.structures import CaseInsensitiveDict

url = "https://beaconcha.in/api/v1/rocketpool/validator/281763"

headers = CaseInsensitiveDict()
headers["accept"] = "application/json"


resp = requests.get(url, headers=headers)

print(resp.status_code)

rp_commission = resp.data_code["minipool_node_fee"]
print(rp_commission)

""" 
from urllib.request import Request, urlopen

# import json
import json
from types import SimpleNamespace

validator_index = 281763
url = f"https://beaconcha.in/api/v1/rocketpool/validator/{validator_index}"
# store the response of URL
req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
json_resp = urlopen(req, timeout=10).read()
print(json_resp)
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
 """
