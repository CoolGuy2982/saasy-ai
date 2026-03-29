import os
import anthropic
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

def generate_and_publish_ad(company_name: str, product_description: str, website_url: str):
    """
    Uses Claude to generate ad copy from the PRD info,
    then publishes it via Google Ads API.
    """
    print(f"[Ads] Generating ad for: {company_name} → {website_url}")

    # Step 1: Claude generates the ad copy
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": f"""Create a Google Search Ad for this company:

Company: {company_name}
Product: {product_description}
Website: {website_url}

Return EXACTLY this format (respect character limits strictly):
HEADLINE_1: (max 30 chars)
HEADLINE_2: (max 30 chars)
HEADLINE_3: (max 30 chars)
DESCRIPTION_1: (max 90 chars)
DESCRIPTION_2: (max 90 chars)
KEYWORDS: keyword1, keyword2, keyword3, keyword4, keyword5"""
        }]
    )

    # Step 2: Parse the response
    ad = {}
    keywords = []
    for line in message.content[0].text.strip().split("\n"):
        if line.startswith("HEADLINE_1:"):   ad["headline1"] = line.split(":", 1)[1].strip()[:30]
        elif line.startswith("HEADLINE_2:"): ad["headline2"] = line.split(":", 1)[1].strip()[:30]
        elif line.startswith("HEADLINE_3:"): ad["headline3"] = line.split(":", 1)[1].strip()[:30]
        elif line.startswith("DESCRIPTION_1:"): ad["desc1"] = line.split(":", 1)[1].strip()[:90]
        elif line.startswith("DESCRIPTION_2:"): ad["desc2"] = line.split(":", 1)[1].strip()[:90]
        elif line.startswith("KEYWORDS:"): keywords = [k.strip() for k in line.split(":", 1)[1].split(",")]

    print(f"[Ads] Generated copy: {ad}")
    print(f"[Ads] Keywords: {keywords}")

    # Step 3: Publish to Google Ads API
    _publish_to_google_ads(ad, keywords, website_url)

    return ad, keywords


def _publish_to_google_ads(ad: dict, keywords: list, website_url: str):
    """Pushes the ad to Google Ads API."""
    customer_id = os.getenv("GOOGLE_ADS_CUSTOMER_ID")
    if not customer_id:
        print("[Ads] GOOGLE_ADS_CUSTOMER_ID not set — skipping publish.")
        return

    try:
        from google.ads.googleads.client import GoogleAdsClient

        gads = GoogleAdsClient.load_from_storage("google-ads.yaml")

        # ── Create campaign ──────────────────────────────────────────────
        camp_svc = gads.get_service("CampaignService")
        camp_op  = gads.get_type("CampaignOperation")
        camp     = camp_op.create
        camp.name = f"Saasy-AI — {ad.get('headline1', 'New Campaign')}"
        camp.advertising_channel_type = gads.enums.AdvertisingChannelTypeEnum.SEARCH
        camp.status = gads.enums.CampaignStatusEnum.PAUSED   # review before going live!
        camp.manual_cpc.enhanced_cpc_enabled = True
        camp.campaign_budget = f"customers/{customer_id}/campaignBudgets/{os.getenv('GOOGLE_ADS_BUDGET_ID', '1')}"

        camp_res = camp_svc.mutate_campaigns(
            customer_id=customer_id, operations=[camp_op]
        ).results[0].resource_name

        # ── Create ad group ───────────────────────────────────────────────
        ag_svc = gads.get_service("AdGroupService")
        ag_op  = gads.get_type("AdGroupOperation")
        ag     = ag_op.create
        ag.name     = "Saasy-AI Ad Group"
        ag.campaign = camp_res
        ag.status   = gads.enums.AdGroupStatusEnum.ENABLED
        ag.type_    = gads.enums.AdGroupTypeEnum.SEARCH_STANDARD
        ag.cpc_bid_micros = 1_000_000  # $1.00 per click

        ag_res = ag_svc.mutate_ad_groups(
            customer_id=customer_id, operations=[ag_op]
        ).results[0].resource_name

        # ── Create responsive search ad ───────────────────────────────────
        ada_svc = gads.get_service("AdGroupAdService")
        ada_op  = gads.get_type("AdGroupAdOperation")
        ada     = ada_op.create
        ada.ad_group = ag_res
        ada.status   = gads.enums.AdGroupAdStatusEnum.ENABLED

        def _txt(t):
            asset = gads.get_type("AdTextAsset")
            asset.text = t
            return asset

        rsa = ada.ad.responsive_search_ad
        rsa.headlines.extend([_txt(ad["headline1"]), _txt(ad["headline2"]), _txt(ad["headline3"])])
        rsa.descriptions.extend([_txt(ad["desc1"]), _txt(ad["desc2"])])
        ada.ad.final_urls.append(website_url)

        ada_svc.mutate_ad_group_ads(customer_id=customer_id, operations=[ada_op])

        # ── Add keywords ──────────────────────────────────────────────────
        kw_svc = gads.get_service("AdGroupCriterionService")
        kw_ops = []
        for kw in keywords:
            kw_op  = gads.get_type("AdGroupCriterionOperation")
            crit   = kw_op.create
            crit.ad_group    = ag_res
            crit.status      = gads.enums.AdGroupCriterionStatusEnum.ENABLED
            crit.keyword.text       = kw
            crit.keyword.match_type = gads.enums.KeywordMatchTypeEnum.PHRASE
            kw_ops.append(kw_op)

        kw_svc.mutate_ad_group_criteria(customer_id=customer_id, operations=kw_ops)

        print(f"[Ads] Campaign created (PAUSED): {camp_res}")

    except Exception as e:
        print(f"[Ads] Google Ads publish failed: {e}")
        