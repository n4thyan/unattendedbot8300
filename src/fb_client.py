"""
Facebook Graph API client for UnattendedBot8300.

Uses official Graph API v26.0 endpoints.
"""

import json
from typing import Any, Optional
from urllib.parse import urlencode

from .config import Config


class FacebookClient:
    """Client for Facebook Graph API operations."""
    
    def __init__(self, config: Config):
        self.config = config
        self.base_url = config.fb_base_url
        self.page_id = config.facebook_page_id
        self.access_token = config.facebook_page_access_token
    
    def _request(self, method: str, endpoint: str, 
                 params: Optional[dict] = None) -> dict:
        """Make an API request to Facebook Graph API."""
        import requests
        
        # Add access token
        if params is None:
            params = {}
        params["access_token"] = self.access_token
        
        url = f"{self.base_url}/{endpoint}?{urlencode(params)}"
        
        headers = {"Content-Type": "application/json"}
        
        if method.upper() == "GET":
            response = requests.get(url, headers=headers)
        elif method.upper() == "POST":
            response = requests.post(url, headers=headers, json=params)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        
        response.raise_for_status()
        result = response.json()
        
        # Handle error response
        if "error" in result:
            raise ValueError(f"Facebook API Error: {result['error']}")
        
        return result
    
    # === Read Operations (Phase 0) ===
    
    def get_page_info(self) -> dict:
        """Get basic Page information."""
        return self._request("GET", self.page_id, {
            "fields": "id,name,about,fan_count,posts_count"
        })
    
    def get_my_posts(self, limit: int = 25) -> dict:
        """Get posts from the Page feed."""
        return self._request("GET", f"{self.page_id}/feed", {
            "fields": "id,message,created_time,updated_time,like_count,comment_count",
            "limit": limit,
            "order": "recent"
        })
    
    def get_post_comments(self, post_id: str, limit: int = 100) -> dict:
        """Get comments on a specific post."""
        return self._request("GET", f"{post_id}/comments", {
            "fields": "id,message,from,created_time,parent,comment_count",
            "limit": limit,
            "order": "reverse chronological"
        })
    
    def get_page_insights(self, period: str = "lifetime") -> dict:
        """Get Page insights (if configured)."""
        # Requires pages_read_engagement permission
        return self._request("GET", f"{self.page_id}/insights", {
            "period": period
        })
    
    # === Write Operations (for Phase 1+) ===
    
    def post_status(self, message: str) -> dict:
        """Post a status update to the Page.
        
        NOTE: This method makes actual writes to Facebook.
        In Phase 0 (dry_run mode), this should not be called.
        """
        return self._request("POST", f"{self.page_id}/feed", {
            "message": message
        })
    
    def post_photo(self, url: str, caption: Optional[str] = None) -> dict:
        """Post a photo to the Page.
        
        NOTE: This method makes actual writes to Facebook.
        """
        params = {"url": url}
        if caption:
            params["caption"] = caption
        return self._request("POST", f"{self.page_id}/photos", params)
    
    def reply_to_comment(self, comment_id: str, message: str) -> dict:
        """Reply to a comment on any object.
        
        NOTE: This method makes actual writes to Facebook.
        Requires pages_manage_engagement permission.
        """
        return self._request("POST", f"{comment_id}/comments", {
            "message": message
        })
    
    def delete_comment(self, comment_id: str) -> dict:
        """Delete a comment (only if owned by the Page).
        
        NOTE: This method makes actual writes to Facebook.
        """
        return self._request("DELETE", comment_id)
    
    def like_post(self, post_id: str) -> dict:
        """Like a Post.
        
        NOTE: This method makes actual writes to Facebook.
        """
        return self._request("POST", f"{post_id}/likes", {})


# === Convenience functions for data extraction ===

def extract_posts_from_response(response: dict) -> list[dict]:
    """Extract posts list from API response."""
    posts = []
    for post in response.get("data", []):
        posts.append({
            "fb_post_id": post.get("id"),
            "message": post.get("message"),
            "created_time": post.get("created_time"),
            "updated_time": post.get("updated_time"),
            "like_count": post.get("like_count", 0),
            "comment_count": post.get("comment_count", 0),
            "raw": post
        })
    return posts


def extract_comments_from_response(response: dict, post_id: str) -> list[dict]:
    """Extract comments list from API response."""
    comments = []
    for comment in response.get("data", []):
        from_info = comment.get("from", {})
        comments.append({
            "fb_comment_id": comment.get("id"),
            "post_id": post_id,
            "parent_comment_id": comment.get("parent", {}).get("id") if comment.get("parent") else None,
            "from_name": from_info.get("name"),
            "from_id": from_info.get("id"),
            "message": comment.get("message"),
            "created_time": comment.get("created_time"),
            "raw": comment
        })
    return comments