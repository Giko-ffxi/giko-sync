local timer   = require('lib.ashita.timer')
local cache   = require('lib.giko.cache')
local config  = require('lib.giko.config')
local common  = require('lib.giko.common')
local monster = require('lib.giko.monster')
local death   = require('lib.giko.death')
local chat    = require('lib.giko.chat')
local json    = require('json.json')
local http    = require('socket.http')
local ltn12   = require("ltn12")

local console = { command= {} }

console.input = function(command, ntype)

    local command, args = string.match(command, '^/giko[%s-]+sync%s+(%w+)(.*)')
    
    local registry = 
    {
        ['start']   = console.command.start,
        ['stop']    = console.command.stop,
        ['status']  = console.command.status
    }

    if command == nil then
        return false
    end

    if registry[command] then
        registry[command](args)
    end
    
    if registry[command] == nil then
        console.command.help()
    end

    return true

end

console.command.load = function()
    
    console.command.start()

end

console.command.start = function(args)

    if config.auth.user ~= nil and config.auth.password  ~= nil and config.tod.get ~= nil and config.tod.set ~= nil and config.tod.interval ~= nil then
        
        ashita.timer.create('sync', common.to_seconds(config.tod.interval), 0, function()
        
            local s_tods = tod.get()
            local c_tods = tod.set(s_tods)
    
        end)
        
    end

end

console.command.stop = function(args)

    ashita.timer.remove_timer('sync')

end

console.command.status = function(args)

    print(string.format('[Sync][%s]', ashita.timer.is_timer('sync') and 'On' or 'Off'))

end

console.command.help = function(args)

    common.help('/giko sync', 
    {
        {'/giko sync start', 'Start the sync.'},
        {'/giko sync stop', 'Stop the sync.'},
        {'/giko sync status', 'Check sync status.'},
    })

end

return console
