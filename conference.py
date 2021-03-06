#!/usr/bin/env python

"""
conference.py -- Udacity conference server-side Python App Engine API;
	uses Google Cloud Endpoints

$Id: conference.py,v 1.25 2014/05/24 23:42:19 wesc Exp wesc $

created by wesc on 2014 apr 21

"""

__author__ = 'wesc+api@google.com (Wesley Chun)'


from datetime import datetime
from datetime import timedelta

import endpoints
from protorpc import messages
from protorpc import message_types
from protorpc import remote

from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import ndb

from models import ConflictException
from models import Profile
from models import ProfileMiniForm
from models import ProfileForm
from models import StringMessage
from models import BooleanMessage
from models import Conference
from models import ConferenceForm
from models import ConferenceForms
from models import ConferenceQueryForm
from models import ConferenceQueryForms
from models import TeeShirtSize

from models import SessionForm
from models import SessionForms
from models import Session

from settings import WEB_CLIENT_ID
from settings import ANDROID_CLIENT_ID
from settings import IOS_CLIENT_ID
from settings import ANDROID_AUDIENCE

from utils import getUserId

EMAIL_SCOPE = endpoints.EMAIL_SCOPE
API_EXPLORER_CLIENT_ID = endpoints.API_EXPLORER_CLIENT_ID
MEMCACHE_ANNOUNCEMENTS_KEY = "RECENT_ANNOUNCEMENTS"
ANNOUNCEMENT_TPL = ('Last chance to attend! The following conferences '
					'are nearly sold out: %s')
					

FEATURED_SPEAKER_KEY = "FEATURED_SPEAKER"
# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

DEFAULTS = {
	"city": "Default City",
	"maxAttendees": 0,
	"seatsAvailable": 0,
	"topics": [ "Default", "Topic" ],
}

OPERATORS = {
			'EQ':   '=',
			'GT':   '>',
			'GTEQ': '>=',
			'LT':   '<',
			'LTEQ': '<=',
			'NE':   '!='
			}

FIELDS =    {
			'CITY': 'city',
			'TOPIC': 'topics',
			'MONTH': 'month',
			'MAX_ATTENDEES': 'maxAttendees',
			}

CONF_GET_REQUEST = endpoints.ResourceContainer(
	message_types.VoidMessage,
	websafeConferenceKey=messages.StringField(1),
)

CONF_POST_REQUEST = endpoints.ResourceContainer(
	ConferenceForm,
	websafeConferenceKey=messages.StringField(1),
)

SESSION_TYPE_REQUEST = endpoints.ResourceContainer(
	type=messages.StringField(1),
	websafeConferenceKey=messages.StringField(2),
)

FEATURED_SPEAKER_REQUEST = endpoints.ResourceContainer(
	speaker=messages.StringField(1),
	websafeConferenceKey=messages.StringField(2),
)

# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -


@endpoints.api(name='conference', version='v1', audiences=[ANDROID_AUDIENCE],
	allowed_client_ids=[WEB_CLIENT_ID, API_EXPLORER_CLIENT_ID, ANDROID_CLIENT_ID, IOS_CLIENT_ID],
	scopes=[EMAIL_SCOPE])
class ConferenceApi(remote.Service):
	"""Conference API v0.1"""

# - - - Conference objects - - - - - - - - - - - - - - - - -

	def _copyConferenceToForm(self, conf, displayName):
		"""Copy relevant fields from Conference to ConferenceForm."""
		cf = ConferenceForm()
		for field in cf.all_fields():
			if hasattr(conf, field.name):
				# convert Date to date string; just copy others
				if field.name.endswith('Date'):
					setattr(cf, field.name, str(getattr(conf, field.name)))
				else:
					setattr(cf, field.name, getattr(conf, field.name))
			elif field.name == "websafeKey":
				setattr(cf, field.name, conf.key.urlsafe())
		if displayName:
			setattr(cf, 'organizerDisplayName', displayName)
		cf.check_initialized()
		return cf


	def _createConferenceObject(self, request):
		"""Create or update Conference object, returning ConferenceForm/request."""
		# preload necessary data items
		user = endpoints.get_current_user()
		if not user:
			raise endpoints.UnauthorizedException('Authorization required')
		user_id = getUserId(user)

		if not request.name:
			raise endpoints.BadRequestException("Conference 'name' field required")

		# copy ConferenceForm/ProtoRPC Message into dict
		data = {field.name: getattr(request, field.name) for field in request.all_fields()}
		del data['websafeKey']
		del data['organizerDisplayName']

		# add default values for those missing (both data model & outbound Message)
		for df in DEFAULTS:
			if data[df] in (None, []):
				data[df] = DEFAULTS[df]
				setattr(request, df, DEFAULTS[df])

		# convert dates from strings to Date objects; set month based on start_date
		if data['startDate']:
			data['startDate'] = datetime.strptime(data['startDate'][:10], "%Y-%m-%d").date()
			data['month'] = data['startDate'].month
		else:
			data['month'] = 0
		if data['endDate']:
			data['endDate'] = datetime.strptime(data['endDate'][:10], "%Y-%m-%d").date()

		# set seatsAvailable to be same as maxAttendees on creation
		if data["maxAttendees"] > 0:
			data["seatsAvailable"] = data["maxAttendees"]
		# generate Profile Key based on user ID and Conference
		# ID based on Profile key get Conference key from ID
		p_key = ndb.Key(Profile, user_id)
		c_id = Conference.allocate_ids(size=1, parent=p_key)[0]
		c_key = ndb.Key(Conference, c_id, parent=p_key)
		data['key'] = c_key
		data['organizerUserId'] = request.organizerUserId = user_id

		# create Conference, send email to organizer confirming
		# creation of Conference & return (modified) ConferenceForm
		Conference(**data).put()
		taskqueue.add(params={'email': user.email(),
			'conferenceInfo': repr(request)},
			url='/tasks/send_confirmation_email'
		)
		return request


	@ndb.transactional()
	def _updateConferenceObject(self, request):
		user = endpoints.get_current_user()
		if not user:
			raise endpoints.UnauthorizedException('Authorization required')
		user_id = getUserId(user)

		# copy ConferenceForm/ProtoRPC Message into dict
		data = {field.name: getattr(request, field.name) for field in request.all_fields()}

		# update existing conference
		conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
		# check that conference exists
		if not conf:
			raise endpoints.NotFoundException(
				'No conference found with key: %s' % request.websafeConferenceKey)

		# check that user is owner
		if user_id != conf.organizerUserId:
			raise endpoints.ForbiddenException(
				'Only the owner can update the conference.')

		# Not getting all the fields, so don't create a new object; just
		# copy relevant fields from ConferenceForm to Conference object
		for field in request.all_fields():
			data = getattr(request, field.name)
			# only copy fields where we get data
			if data not in (None, []):
				# special handling for dates (convert string to Date)
				if field.name in ('startDate', 'endDate'):
					data = datetime.strptime(data, "%Y-%m-%d").date()
					if field.name == 'startDate':
						conf.month = data.month
				# write to Conference object
				setattr(conf, field.name, data)
		conf.put()
		prof = ndb.Key(Profile, user_id).get()
		return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


	@endpoints.method(ConferenceForm, ConferenceForm, path='conference',
			http_method='POST', name='createConference')
	def createConference(self, request):
		"""Create new conference."""
		return self._createConferenceObject(request)


	@endpoints.method(CONF_POST_REQUEST, ConferenceForm,
			path='conference/{websafeConferenceKey}',
			http_method='PUT', name='updateConference')
	def updateConference(self, request):
		"""Update conference w/provided fields & return w/updated info."""
		return self._updateConferenceObject(request)


	@endpoints.method(CONF_GET_REQUEST, ConferenceForm,
			path='conference/{websafeConferenceKey}',
			http_method='GET', name='getConference')
	def getConference(self, request):
		"""Return requested conference (by websafeConferenceKey)."""
		# get Conference object from request; bail if not found
		conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
		if not conf:
			raise endpoints.NotFoundException(
				'No conference found with key: %s' % request.websafeConferenceKey)
		prof = conf.key.parent().get()
		# return ConferenceForm
		return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


	@endpoints.method(message_types.VoidMessage, ConferenceForms,
			path='getConferencesCreated',
			http_method='POST', name='getConferencesCreated')
	def getConferencesCreated(self, request):
		"""Return conferences created by user."""
		# make sure user is authed
		user = endpoints.get_current_user()
		if not user:
			raise endpoints.UnauthorizedException('Authorization required')
		user_id = getUserId(user)

		# create ancestor query for all key matches for this user
		confs = Conference.query(ancestor=ndb.Key(Profile, user_id))
		prof = ndb.Key(Profile, user_id).get()
		# return set of ConferenceForm objects per Conference
		return ConferenceForms(
			items=[self._copyConferenceToForm(conf, getattr(prof, 'displayName')) for conf in confs]
		)


	def _getQuery(self, request):
		"""Return formatted query from the submitted filters."""
		q = Conference.query()
		inequality_filter, filters = self._formatFilters(request.filters)

		# If exists, sort on inequality filter first
		if not inequality_filter:
			q = q.order(Conference.name)
		else:
			q = q.order(ndb.GenericProperty(inequality_filter))
			q = q.order(Conference.name)

		for filtr in filters:
			if filtr["field"] in ["month", "maxAttendees"]:
				filtr["value"] = int(filtr["value"])
			formatted_query = ndb.query.FilterNode(filtr["field"], filtr["operator"], filtr["value"])
			q = q.filter(formatted_query)
		return q


	def _formatFilters(self, filters):
		"""Parse, check validity and format user supplied filters."""
		formatted_filters = []
		inequality_field = None

		for f in filters:
			filtr = {field.name: getattr(f, field.name) for field in f.all_fields()}

			try:
				filtr["field"] = FIELDS[filtr["field"]]
				filtr["operator"] = OPERATORS[filtr["operator"]]
			except KeyError:
				raise endpoints.BadRequestException("Filter contains invalid field or operator.")

			# Every operation except "=" is an inequality
			if filtr["operator"] != "=":
				# check if inequality operation has been used in previous filters
				# disallow the filter if inequality was performed on a different field before
				# track the field on which the inequality operation is performed
				if inequality_field and inequality_field != filtr["field"]:
					raise endpoints.BadRequestException("Inequality filter is allowed on only one field.")
				else:
					inequality_field = filtr["field"]

			formatted_filters.append(filtr)
		return (inequality_field, formatted_filters)


	@endpoints.method(ConferenceQueryForms, ConferenceForms,
			path='queryConferences',
			http_method='POST',
			name='queryConferences')
	def queryConferences(self, request):
		"""Query for conferences."""
		conferences = self._getQuery(request)

		# need to fetch organiser displayName from profiles
		# get all keys and use get_multi for speed
		organisers = [(ndb.Key(Profile, conf.organizerUserId)) for conf in conferences]
		profiles = ndb.get_multi(organisers)

		# put display names in a dict for easier fetching
		names = {}
		for profile in profiles:
			names[profile.key.id()] = profile.displayName

		# return individual ConferenceForm object per Conference
		return ConferenceForms(
				items=[self._copyConferenceToForm(conf, names[conf.organizerUserId]) for conf in \
				conferences]
		)


# - - - Profile objects - - - - - - - - - - - - - - - - - - -

	def _copyProfileToForm(self, prof):
		"""Copy relevant fields from Profile to ProfileForm."""
		# copy relevant fields from Profile to ProfileForm
		pf = ProfileForm()
		for field in pf.all_fields():
			if hasattr(prof, field.name):
				# convert t-shirt string to Enum; just copy others
				if field.name == 'teeShirtSize':
					setattr(pf, field.name, getattr(TeeShirtSize, getattr(prof, field.name)))
				else:
					setattr(pf, field.name, getattr(prof, field.name))
		pf.check_initialized()
		return pf


	def _getProfileFromUser(self):
		"""Return user Profile from datastore, creating new one if non-existent."""
		# make sure user is authed
		user = endpoints.get_current_user()
		if not user:
			raise endpoints.UnauthorizedException('Authorization required')

		# get Profile from datastore
		user_id = getUserId(user)
		p_key = ndb.Key(Profile, user_id)
		profile = p_key.get()
		# create new Profile if not there
		if not profile:
			profile = Profile(
				key = p_key,
				displayName = user.nickname(),
				mainEmail= user.email(),
				teeShirtSize = str(TeeShirtSize.NOT_SPECIFIED),
			)
			profile.put()

		return profile      # return Profile


	def _doProfile(self, save_request=None):
		"""Get user Profile and return to user, possibly updating it first."""
		# get user Profile
		prof = self._getProfileFromUser()

		# if saveProfile(), process user-modifyable fields
		if save_request:
			for field in ('displayName', 'teeShirtSize'):
				if hasattr(save_request, field):
					val = getattr(save_request, field)
					if val:
						setattr(prof, field, str(val))
						#if field == 'teeShirtSize':
						#    setattr(prof, field, str(val).upper())
						#else:
						#    setattr(prof, field, val)
						prof.put()

		# return ProfileForm
		return self._copyProfileToForm(prof)


	@endpoints.method(message_types.VoidMessage, ProfileForm,
			path='profile', http_method='GET', name='getProfile')
	def getProfile(self, request):
		"""Return user profile."""
		return self._doProfile()


	@endpoints.method(ProfileMiniForm, ProfileForm,
			path='profile', http_method='POST', name='saveProfile')
	def saveProfile(self, request):
		"""Update & return user profile."""
		return self._doProfile(request)


# - - - Announcements - - - - - - - - - - - - - - - - - - - -

	@staticmethod
	def _cacheAnnouncement():
		"""Create Announcement & assign to memcache; used by
		memcache cron job & putAnnouncement().
		"""
		confs = Conference.query(ndb.AND(
			Conference.seatsAvailable <= 5,
			Conference.seatsAvailable > 0)
		).fetch(projection=[Conference.name])

		if confs:
			# If there are almost sold out conferences,
			# format announcement and set it in memcache
			announcement = ANNOUNCEMENT_TPL % (
				', '.join(conf.name for conf in confs))
			memcache.set(MEMCACHE_ANNOUNCEMENTS_KEY, announcement)
		else:
			# If there are no sold out conferences,
			# delete the memcache announcements entry
			announcement = ""
			memcache.delete(MEMCACHE_ANNOUNCEMENTS_KEY)

		return announcement


	@endpoints.method(message_types.VoidMessage, StringMessage,
			path='conference/announcement/get',
			http_method='GET', name='getAnnouncement')
	def getAnnouncement(self, request):
		"""Return Announcement from memcache."""
		return StringMessage(data=memcache.get(MEMCACHE_ANNOUNCEMENTS_KEY) or "")


# - - - Registration - - - - - - - - - - - - - - - - - - - -

	@ndb.transactional(xg=True)
	def _conferenceRegistration(self, request, reg=True):
		"""Register or unregister user for selected conference."""
		retval = None
		prof = self._getProfileFromUser() # get user Profile

		# check if conf exists given websafeConfKey
		# get conference; check that it exists
		wsck = request.websafeConferenceKey
		conf = ndb.Key(urlsafe=wsck).get()
		if not conf:
			raise endpoints.NotFoundException(
				'No conference found with key: %s' % wsck)

		# register
		if reg:
			# check if user already registered otherwise add
			if wsck in prof.conferenceKeysToAttend:
				raise ConflictException(
					"You have already registered for this conference")

			# check if seats avail
			if conf.seatsAvailable <= 0:
				raise ConflictException(
					"There are no seats available.")

			# register user, take away one seat
			prof.conferenceKeysToAttend.append(wsck)
			conf.seatsAvailable -= 1
			retval = True

		# unregister
		else:
			# check if user already registered
			if wsck in prof.conferenceKeysToAttend:

				# unregister user, add back one seat
				prof.conferenceKeysToAttend.remove(wsck)
				conf.seatsAvailable += 1
				retval = True
			else:
				retval = False

		# write things back to the datastore & return
		prof.put()
		conf.put()
		return BooleanMessage(data=retval)


	@endpoints.method(message_types.VoidMessage, ConferenceForms,
			path='conferences/attending',
			http_method='GET', name='getConferencesToAttend')
	def getConferencesToAttend(self, request):
		"""Get list of conferences that user has registered for."""
		prof = self._getProfileFromUser() # get user Profile
		conf_keys = [ndb.Key(urlsafe=wsck) for wsck in prof.conferenceKeysToAttend]
		conferences = ndb.get_multi(conf_keys)

		# get organizers
		organisers = [ndb.Key(Profile, conf.organizerUserId) for conf in conferences]
		profiles = ndb.get_multi(organisers)

		# put display names in a dict for easier fetching
		names = {}
		for profile in profiles:
			names[profile.key.id()] = profile.displayName

		# return set of ConferenceForm objects per Conference
		return ConferenceForms(items=[self._copyConferenceToForm(conf, names[conf.organizerUserId])\
		 for conf in conferences]
		)


	@endpoints.method(CONF_GET_REQUEST, BooleanMessage,
			path='conference/{websafeConferenceKey}',
			http_method='POST', name='registerForConference')
	def registerForConference(self, request):
		"""Register user for selected conference."""
		return self._conferenceRegistration(request)


	@endpoints.method(CONF_GET_REQUEST, BooleanMessage,
			path='conference/{websafeConferenceKey}',
			http_method='DELETE', name='unregisterFromConference')
	def unregisterFromConference(self, request):
		"""Unregister user for selected conference."""
		return self._conferenceRegistration(request, reg=False)


	@endpoints.method(message_types.VoidMessage, ConferenceForms,
			path='filterPlayground',
			http_method='GET', name='filterPlayground')
	def filterPlayground(self, request):
		"""Filter Playground"""
		q = Conference.query()
		# field = "city"
		# operator = "="
		# value = "London"
		# f = ndb.query.FilterNode(field, operator, value)
		# q = q.filter(f)
		q = q.filter(Conference.city=="London")
		q = q.filter(Conference.topics=="Medical Innovations")
		q = q.filter(Conference.month==6)

		return ConferenceForms(
			items=[self._copyConferenceToForm(conf, "") for conf in q]
		)
		
		
		

#######################
# Session stuff below
#######################


	def _copySessionToForm(self, session):
		"""Copy relevant fields from Session to SessionForm."""
		sf = SessionForm()
		for field in sf.all_fields():
			if hasattr(session, field.name):
				# convert date/time to string
				if (field.name.endswith('date') or
					field.name.endswith('Time') or
					field.name.endswith('duration')):
					setattr(sf, field.name, str(getattr(session, field.name)))
				else:
					setattr(sf, field.name, getattr(session, field.name))
					
			if field.name == "websafeSessionKey":
				setattr(sf, field.name, session.key.urlsafe())
				
			if field.name == "websafeConferenceKey":
				conf = session.key.parent().get()
				setattr(sf, field.name, conf.key.urlsafe())
		
		sf.check_initialized()
		return sf


	@endpoints.method(CONF_GET_REQUEST, SessionForms,
			path='conference/{websafeConferenceKey}/sessions',
			http_method='GET', name='getConferenceSessions')
	def getConferenceSessions(self, request):
		"""Return conference sessions (given websafeConferenceKey)."""
		# get Conference object from request; bail if not found
		conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
		
		if not conf:
			raise endpoints.NotFoundException(
				'No conference found with key: %s' % request.websafeConferenceKey)
				
		conf_key = conf.key
				
		# create ancestor query for all key matches for this conf
		conf_sessions = Session.query(ancestor=conf_key)

		# return set of SessionForm objects 
		return SessionForms(
			items=[self._copySessionToForm(session) for session in conf_sessions]
		)

		
		
		

	@endpoints.method(SESSION_TYPE_REQUEST, SessionForms,
			path='conference/{websafeConferenceKey}/sessionsByType',
			http_method='GET', name='getConferenceSessionsByType')
	def getConferenceSessionsByType(self, request):
		"""Return conference sessions (given websafeConferenceKey)."""
		# get Conference object from request; bail if not found
		conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
		
		if not conf:
			raise endpoints.NotFoundException(
				'No conference found with key: %s' % request.websafeConferenceKey)
				
		conf_key = conf.key
				
		# create ancestor query for all key matches for this conf
		conf_sessions = Session.query(ancestor=conf_key)
		
		# filter by type
		conf_sessions = conf_sessions.filter(Session.typeOfSession==request.type)

		# return set of SessionForm objects 
		return SessionForms(
			items=[self._copySessionToForm(session) for session in conf_sessions]
		)

		
		
	def _createSessionObject(self, request):
		"""Create Session object, returning SessionForm/request."""
		# preload necessary data items
		user = endpoints.get_current_user()
		if not user:
			raise endpoints.UnauthorizedException('Authorization required')
		user_id = getUserId(user)
		
		
		# get the conference object user is trying to add a session to
		conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
		
		# check that conference exists
		if not conf:
			raise endpoints.NotFoundException(
				'No conference found with key: %s' % request.websafeConferenceKey)

		# check that user is the owner
		if user_id != conf.organizerUserId:
			raise endpoints.ForbiddenException(
				'Only the owner can update the conference.')

		
		if not request.name:
			raise endpoints.BadRequestException("Session 'name' field required")
		
		
		# copy SessionForm data into dict, except websafe keys
		data = {}
		for field in request.all_fields():
			if (field.name != 'websafeConferenceKey' and 
				field.name != 'websafeSessionKey'):
				data[field.name] = getattr(request, field.name)


		# convert dates and times from strings to date/time objects
		if data['date']:
			data['date'] = datetime.strptime(data['date'][:10], "%Y-%m-%d").date()
		if data['startTime']:
			data['startTime'] = datetime.strptime(data['startTime'][:5], "%H:%M").time()
		if data['duration']:
			data['duration'] = datetime.strptime(data['duration'][:5], "%H:%M").time()
			

		# create session key with ancestor relationship 
		conf_key = conf.key
		session_id = Session.allocate_ids(size=1, parent=conf_key)[0]
		session_key = ndb.Key(Session, session_id, parent=conf_key)
		data['key'] = session_key
		#data['websafeSessionKey'] = session_key.urlsafe()
		
		
		# create Session
		Session(**data).put()
		
		
		# if speaker is defined, check if they qualify as Featured Speaker
		if data['speaker']:
			taskqueue.add(params={'speaker': data['speaker'],
				'websafeConferenceKey': request.websafeConferenceKey},
				url='/tasks/get_featured_speaker')
		

		return request
		

	@endpoints.method(SessionForm, SessionForm, path='createSession',
			http_method='POST', name='createSession')
	def createSession(self, request):
		"""Create new Session."""
		return self._createSessionObject(request)
		
		
		
		
	@endpoints.method(StringMessage, SessionForms, path='getSessionsBySpeaker',
			http_method='GET', name='getSessionsBySpeaker')
	def getSessionsBySpeaker(self, request):
		"""Get Sessions of a particular speaker."""
		
		q = Session.query()

		q = q.filter(Session.speaker==request.data)
		
		# return set of SessionForm objects 
		return SessionForms(
			items=[self._copySessionToForm(session) for session in q]
		)

		
		

	@endpoints.method(StringMessage, SessionForms, path='getSessionsByDate',
			http_method='GET', name='getSessionsByDate')
	def getSessionsByDate(self, request):
		"""Get Sessions on a particular date."""
		
		q = Session.query()
		
		# convert input string to date object 
		date = datetime.strptime(request.data[:10], "%Y-%m-%d").date()

		q = q.filter(Session.date==date)
		
		# return set of SessionForm objects 
		return SessionForms(
			items=[self._copySessionToForm(session) for session in q]
		)
		
		
		
	@endpoints.method(message_types.VoidMessage, SessionForms, path='getSessionsStartingSoon',
			http_method='GET', name='getSessionsStartingSoon')
	def getSessionsStartingSoon(self, request):
		"""Get Sessions starting within 2 hours."""
		
		q = Session.query()
		
		# get current time
		time_now = datetime.now()
		
		# create object to represent a 2 hour time period
		two_hours = timedelta(minutes = 120)
		
		# create a list to contain sessions that are starting soon
		starting_soon = SessionForms()
		
		for session in q:
			# form a datetime object to represent the start date & time
			d = session.date
			t = session.startTime
			start_date_time = datetime(d.year, d.month, d.day, t.hour, t.minute)
			
			if (time_now < start_date_time):
				if (start_date_time - time_now < two_hours):
					starting_soon.items.append(self._copySessionToForm(session))
				
		return starting_soon

		
		
	@endpoints.method(message_types.VoidMessage, SessionForms, path='getSessionsBefore7NotWorkshop',
			http_method='GET', name='getSessionsBefore7NotWorkshop')
	def getSessionsBefore7NotWorkshop(self, request):
		"""Get Sessions starting before 7pm that are not workshop."""
		
		q = Session.query()
		q = q.order(Session.startTime)
		
		# get sessions before 7pm
		sevenpm = datetime.strptime('19:00', "%H:%M").time()
		
		q = q.filter(Session.startTime < sevenpm)
		
		# from the sessions before 7pm, choose the ones that are not workshop
		# and put them in a list 
		non_workshops = SessionForms()
		
		for session in q:
			if session.typeOfSession != 'workshop':
				non_workshops.items.append(self._copySessionToForm(session))
				
		return non_workshops



	@endpoints.method(StringMessage, SessionForm, path='addSessionToWishList',
			http_method='PUT', name='addSessionToWishList')
	def addSessionToWishList(self, request):
		"""Add Session to user's wishlist."""
		
		prof = self._getProfileFromUser() # get user Profile

		# check if session exists given websafeSessionKey
		websafe_session_key = request.data
		session = ndb.Key(urlsafe=websafe_session_key).get()
		if not session:
			raise endpoints.NotFoundException(
				'No session found with key: %s' % websafe_session_key)

		# check if user already has session in wishlist
		if websafe_session_key in prof.sessionKeysInWishList:
			raise ConflictException(
				"You have already have this session in your wishlist")

		# add session to wishlist
		prof.sessionKeysInWishList.append(websafe_session_key)
		prof.put()

		# show the newly added session's details
		return self._copySessionToForm(session)
		
		
		
		
	@endpoints.method(StringMessage, SessionForm, path='deleteSessionInWishList',
			http_method='DELETE', name='deleteSessionInWishList')
	def deleteSessionInWishList(self, request):
		"""Delete Session from user's wishlist."""
		
		prof = self._getProfileFromUser() # get user Profile

		# check if session exists given websafeSessionKey
		websafe_session_key = request.data
		session = ndb.Key(urlsafe=websafe_session_key).get()
		if not session:
			raise endpoints.NotFoundException(
				'No session found with key: %s' % websafe_session_key)

		# check if user already has session in wishlist
		if not (websafe_session_key in prof.sessionKeysInWishList):
			raise ConflictException(
				"You don't have this session in your wishlist")

		# remove session from wishlist
		prof.sessionKeysInWishList.remove(websafe_session_key)
		prof.put()

		# show the details of the deleted session
		return self._copySessionToForm(session)
		
		
		
		
		
	
	@endpoints.method(message_types.VoidMessage, SessionForms, path='getSessionsInWishList',
			http_method='GET', name='getSessionsInWishList')
	def getSessionsInWishList(self, request):
		"""Get Sessions in user's wishlist."""
		
		prof = self._getProfileFromUser() # get user Profile
		session_keys = [ndb.Key(urlsafe=websafe_session_key) for websafe_session_key in prof.sessionKeysInWishList]
		sessions = ndb.get_multi(session_keys)

		# return set of SessionForm objects 
		return SessionForms(
			items=[self._copySessionToForm(session) for session in sessions]
		)
		
		
		
		
	@staticmethod
	def _getFeaturedSpeaker(speaker, websafeConferenceKey):
		"""Checks if the given speaker has 2 or more sessions 
			in the given conference and sets a memcache message
			accordingly."""
		
		# get the list of sessions conducted by the speaker
		# for the given conference
		
		
		# get Conference object from request; bail if not found
		conf = ndb.Key(urlsafe=websafeConferenceKey).get()
		
		if not conf:
			raise endpoints.NotFoundException(
				'No conference found with key: %s' % request.websafeConferenceKey)
				
				
		# create ancestor query for all key matches for this conf
		conf_sessions = Session.query(ancestor=conf.key)
		
		# filter by speaker
		conf_sessions = conf_sessions.filter(Session.speaker==speaker)
		
		sessions = conf_sessions.fetch()
		
		if len(sessions) >= 2:
			message = 'Featured Speaker - %s has these sessions: %s ' % (
				speaker, ', '.join(s.name for s in sessions))
			memcache.set(FEATURED_SPEAKER_KEY, message)
		else:
			message = ""
			memcache.delete(FEATURED_SPEAKER_KEY)

		return StringMessage(data=message)
		
		
		
	@endpoints.method(FEATURED_SPEAKER_REQUEST, StringMessage, path='getFeaturedSpeaker',
			http_method='GET', name='getFeaturedSpeaker')
	def getFeaturedSpeaker(self, request):
		"""Check if speaker has multiple sessions in the given conference.
			If yes, put a message in memcache."""
		return self._getFeaturedSpeaker(request.speaker, request.websafeConferenceKey)
		
		
		
api = endpoints.api_server([ConferenceApi]) # register API
