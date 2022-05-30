import os
from dotenv import load_dotenv
import datetime as dt
import urllib3
import json
from urllib.parse import urlencode

def main():
    load_dotenv("../.env/.env")
    auth_token = os.getenv('BEARER')

    # Nunmber of hours back to query from Twitter API 
    QUERYTIME = 168
    # Hashtag to search for
    HASHTAG = "awsoutage"
    HASHTAG = "awsoutage"
    # Number of results to return, 100 max.  Need to use next_token to get more
    MAX_RESULTS = 100

    dtNow = dt.datetime.now(dt.timezone.utc) 
    start_time = (dtNow - dt.timedelta(hours=QUERYTIME)).isoformat()

    next_token=""
    again=True
    while again:
        url = "https://api.twitter.com/2/tweets/search/recent?"
        querystring = {"query":HASHTAG,
                    "max_results":MAX_RESULTS,
                    "tweet.fields":"created_at",
                    "user.fields":"created_at"
                    }

        # The first pass needs a start_time to lookup.  Each time we query the next 100
        # results, we pass the next_token Twitter gave us instead of the start_time
        if next_token:
            querystring["next_token"] = next_token
        else:
            querystring["start_time"] = start_time

        headers = {'Authorization': 'Bearer '+ auth_token}
        http = urllib3.PoolManager()
        url = url + urlencode(querystring)
        print(url)

        res = http.request(method='GET',
                        url=url,
                        headers=headers)
        if res.status != 200:
            print("Exited with status: ", urllib3.exceptions.HTTPError(res.status))
            print(res.data)
            return
        tweets_data = json.loads(res.data)
        
        count = tweets_data['meta']['result_count']
        if count == 0:
            print(tweets_data)
            print("No results returned")
            return
        else:
            print("Twitter API returned " + str(count) + " results.")

        # If we made it this far, there are are more results to go get so we grab the next_token
        if "next_token" in tweets_data["meta"]:
            next_token = tweets_data["meta"]["next_token"]
            print(next_token)
        else:
            again = False
 
    exit(0)

if __name__ == "__main__":
    main()
