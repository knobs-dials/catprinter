
This prints images and text to a cat-shaped printer - tested towards the printer I have, which reports as an MX06.

A little more technically, it is:
- a HTTP server that actually does the bluetooth contacting
- that serves a web page to contact itself for you to poke at 
  (meant to be viewed from the same host, but you could make this a network service)


This code started off from https://gist.github.com/mpomery/6514e521d3d03abce697409609978ede 
and an interface that loosely imitates https://github.com/NaitLee/Cat-Printer but with simpler code (that is also somewhat slower).

Requirements:
- python3
  - bleak library (for bluetooth)
  - PIL library
  - flask (could be stripped out)
- bluetooth hardware (probably a laptop, though this was actually developed on a desktop with a USB dongle)
