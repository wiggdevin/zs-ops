-- finder_views.applescript - set Finder list view, sorted by Date Modified (newest first)
-- for every folder path passed as an argument. Opens each folder in a window, sets the view,
-- reads the sort column back, closes the window. Prints "<path>\t<sort column>" per folder.
-- Usage: osascript finder_views.applescript /path/one /path/two ...
-- Date Modified is a descending sort when first chosen, which is what "newest first" needs.
on run argv
	set report to ""
	tell application "Finder"
		repeat with p in argv
			try
				set theFolder to (POSIX file (p as text)) as alias
				set theWindow to make new Finder window to folder theFolder
				set current view of theWindow to list view
				set opts to list view options of theWindow
				set sort column of opts to (column id modification date column of opts)
				set sc to sort column of opts
				set cn to name of sc as text
				set dn to sort direction of sc as text
				set sortInfo to cn & " / " & dn
				delay 0.4
				close theWindow
				set report to report & (p as text) & tab & sortInfo & linefeed
			on error errMsg
				try
					close front Finder window
				end try
				set report to report & (p as text) & tab & "ERROR " & errMsg & linefeed
			end try
		end repeat
	end tell
	return report
end run
