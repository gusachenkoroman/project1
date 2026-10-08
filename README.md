# project1
I want to back up pages from Yandex.Wiki (https://yandex.ru/support/wiki/ru/overview).
Our company maintains documentation there, and there is a risk of data loss.
Two tools are required; create each tool in its own directory.
Execution environment and technologies:
Operating System: Windows
Language: Python

General concept of the tools:
1. Tool 1 - Save_script: Saves Yandex.Wiki pages into separate files within the `WikiBackupPages` directory so they can later be recreated (published to Yandex.Wiki) by the other tool. Saving begins at the page specified as a parameter when calling the tool—this parameter is `StartPageToSave`.
2. Tool 2 - Create_script: Reads the saved Yandex.Wiki page files (created by Tool 1) from the `WikiBackupPages` directory. Since the pages may be linked to one another and the creation order matters, it creates them starting from the page specified as a parameter—this parameter is `StartPageToCreate`.

Requirements for Tool 1:
1. Mandatory user parameters:
1.1. `StartPageToSave`: The starting URL from which to begin saving pages (including the starting page itself).
1.2. `WikiBackupPages`: The directory for saving the pages.
2. Operational algorithm:
2.1. Upon receiving the starting page, save all its content using the full capabilities of the API (https://yandex.ru/support/wiki/ru/api-ref/).
2.2. Retrieve information about all child pages.
2.3. Initiate the saving of all content for the discovered child pages using the full API capabilities; traverse all child subpages and their respective subpages starting from the specified `StartPage` URL and save them.
2.4. Log the attempt to save each page in a way that allows you to identify the cause of any error and modify the tool's logic. Include instructions in the tool's `README.md` on how to analyze the logs for the selected format.
2.5. If an attempt to save a page fails, do not stop; continue the saving process while recording failures. Simultaneously, generate a separate file listing all successes and failures in the format: `page URL - status = {ok, error}`. Include instructions in the `README.md` specifying the location of the results file.
Requirements for Tool 2:
1. Mandatory user parameters:
1.1. StartPageToCreate: the starting directory where the saved pages should be created.
1.2. WikiBackupPages: the directory containing the saved pages.
2. Operational algorithm:
2.1. Log the attempt to create each page in a way that allows you to identify the cause of any error and modify the tool's logic. Include instructions in the tool's `README.md` on how to analyze the logs for the selected format.
2.2. If an attempt to create a page fails, do not stop; continue the process while recording failures. Simultaneously, generate a separate file listing all successes and failures in the format: `page URL - status = {ok, error}`. Include instructions in the `README.md` specifying the location of the results file.