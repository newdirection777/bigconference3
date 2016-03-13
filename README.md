###Welcome to Conference Central, a web app for managing conferences.###

This app allows you to view, create, and modify conferences and conference
sessions.

###How to use this app:###

You can access this app through the web GUI by pointing your browser at:

https://bigconference3.appspot.com

Or, you can access the API by pointing your web browser to:

https://bigconference3.appspot.com/_ah/api/explorer 
and clicking 'conference API'

You need to be logged in to your Google account to be able to create and edit
items.  The web GUI allows you to view, create, and edit conferences.  The 
API allows you to view, create, and edit both conferences and sessions.


###Design notes:###

The Session class contains the fields specified in the problem description
represented using corresponding NDB data types.  The 'date' field is represented
using DateProperty, and TimeProperty is used for 'startTime' and 'duration'.
Use of these date/time types is helpful for some of the session queries that
do comparisons based on date/time (like getSessionsBefore7NotWorkshop, 
getSessionsStartingSoon).

The SessionForm class contains the same fields as Session, with the addition of
the websafe keys for the session and its parent conference.

The speaker is represented as a string inside the Session class.  That's
because for this particular use case the only thing a Session object
needs to know about the speaker is their name.  We may want to maintain
more detailed info about the speaker.  For example we may want to let the
users look at a speaker's background and qualifications.  That will help
the user decide if they want to attend a session with that speaker.
In cases like that we could introduce a separate data class called Speaker.


###Task notes:###

a) For Task 3, the following additional queries are implemented:

getSessionsByDate - shows all the sessions happening on a specific day.  This 
is helpful for attendees in planning their time at a conference.  For example,
if an attendee only has one day available to attend sessions, then this query
will tell them what sessions are available that day.

getSessionsStartingSoon - shows all the sessions that are starting in two hours.
This is also meant to help attendees manage their time.  This query can be set 
up to run periodically as a cron job and issue an alert for sessions that will 
start soon.

b) For the query problem: the issue is that the Datastore does not allow
inequality filters on more than one property in a single query.

To get around this, I first do a query for Sessions starting before 7pm
and store the results.  I then use a for loop to go through the query
results, choosing the ones that are non-workshop and storing them in
a separate list.  When the loop terminates, the list containing the
non-workshops is returned.

This is implemented in the method GetSessionsBefore7NotWorkshop.

c) getFeaturedSpeaker puts a message in memcache when a featured speaker is
found.  Memcache provides fast data access.  However, the downside is that its
contents may be overwritten at any time.  That is fine however, for storing the  
featured speaker data which is non-critical (it doesn't really matter if it gets
overwritten) and transient.  The memcache message can be displayed to the user 
as an alert in the GUI front end.


