# Iran Think-Tank Monitor

A small automated website that checks selected think-tank RSS feeds every six
hours and displays Iran-related items published during the previous 24 hours.

## Set up without running code

1. Sign in to GitHub and create a new **public** repository named
   `iran-think-tank-monitor`.
2. Choose **uploading an existing file**.
3. Upload the contents of this package, including the `.github` folder.
   The easiest method is to unzip the package and drag all visible files and
   folders into the upload page.
4. Commit the files.
5. Open the repository's **Actions** tab.
6. Select **Update Iran monitor**, click **Run workflow**, and let it complete.
7. Open **Settings → Pages**.
8. Under **Build and deployment**, choose **Deploy from a branch**.
9. Select branch **main**, folder **/docs**, then click **Save**.

GitHub will show the website address on the Pages settings screen. Bookmark it.

## What happens afterwards

- GitHub runs the scanner approximately every six hours.
- It checks the previous 24 hours.
- The website provides search and think-tank filtering.
- Edit `sources.json` in GitHub to add or remove sources.
- Open Actions and use **Run workflow** whenever you want an immediate refresh.

## Limitations

This is a strong starting point, not a guarantee of every think-tank publication.
Some sites have no RSS feed, block automated requests, or publish incomplete
metadata. RSS-focused monitoring is deliberately conservative and avoids
aggressive scraping.
