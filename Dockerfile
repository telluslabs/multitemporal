FROM ubuntu:16.04

WORKDIR /multitemporal
COPY . /multitemporal

RUN echo "deb http://ppa.launchpad.net/ubuntugis/ppa/ubuntu xenial main" >> \
       /etc/apt/sources.list \
    && apt-key adv --keyserver keyserver.ubuntu.com --recv-keys 314DF160 \
    && apt-get -y update \
    && apt-get install -y \
    gcc \
    python \
    python-pip \
    python-numpy \
    python-scipy \
    python-pandas \
    python-gdal \
    libgdal-dev \
    gdal-bin \
    emacs-nox \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get -y autoremove \
    && apt-get -y autoclean

RUN pip install -r requirements.txt \
    && pip install -e .

VOLUME data
