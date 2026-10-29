def get_correct_extension_path(browser, extension):
    """
    This function returns the correct path for the browser extension
    Input:
        - browser : str, the browser name (firefox or other)
        - extension : str, file name of extension without file type
    """
    if not extension:
        return None
    file_path = "firefox_extensions" if browser == "firefox" else "chrome_extensions"
    extension_type = "/"
    return f"/home/tiesd/thesis_2026/Thesis_Ties_2026/automated_testing/{file_path}/{extension}{extension_type}"
