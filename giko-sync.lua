package.path = (string.gsub(_addon.path, '[^\\]+\\?$', '')) .. 'giko-common\\' .. '?.lua;' .. package.path

_addon.author 	= 'giko'
_addon.name 	= 'giko-sync'
_addon.version 	= '1.0.5'

console = require('core.console')
tod     = require('core.tod')

ashita.register_event('load', console.command.load)
ashita.register_event('command', console.input)