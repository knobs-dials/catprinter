# What

This prints images and text to a cat-shaped printer. 
- lets you input text field (will render that text)
- lets you input images - plus zoom, brightness, contast, and rotation options

![what it looks like](what.jpg)

Tested 
* on win, lin, and osx; 
* towards the printer I have, which reports as an MX06, and the base bluetooth code this is based on mentioned a handful of others;
* not yet tested on all browsers.


# How (install)

Requirements:
- **python3**
  - **`pillow`** library (for image processing)
  - **`bleak`** library (for bluetooth)
  - **`flask`** (could be stripped out)
- **bluetooth hardware** (probably a laptop, though this was actually developed on a windows desktop with a USB dongle)

In ubuntu, either use a virtualenv, or do a system-wide install -- ubuntu now makes you use package installs rather than pip installs, so use `apt install python3-pillow python3-bleak python3-flask`

In other linux, windows, and OSX, `pip install -r requirements.txt` should work - assuming you already had python.


# How (run)

Run `python catprint.py`, it should run the little server that find the first applicable printer and start a browser tab for you to poke at to print to it.

(in windows, you can avoid opening a cmd window by running `run.bat`)

<!-- -->

As long as that server is running, the bluetooth connection stays established and the cat printer awake.
This is in part because one of its uses would be to leave it on a charger and make it the already-mentioned physical notification thing, 
[like the author of that gist did](https://dev.to/mitchpommers/my-textable-cat-printer-18ge) - see also the original code mentioned below.


# What, more technically

A little more technically, it is:
- The server both handles bluetooth communication, image conversion, and is a HTTP server...
- ...that serves a web page, to contact itself for you to poke at 

As-is it's meant to be viewed from the same host,
but it was written in a way that makes it easy enough 
to make this a network service so that you can e.g. have a printer be a physical notification thing.

This code started off from [this gist](https://gist.github.com/mpomery/6514e521d3d03abce697409609978ede) but adds some more robustness and flexibility, and an interface that loosely imitates [this project's interface](https://github.com/NaitLee/Cat-Printer) but with simpler, slower code.

Most image processing is done on the browser side; the resulting image is then sent to the backend.



# TODO

Allow rotation, controlled from the UI.

