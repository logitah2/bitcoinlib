# -*- coding: utf-8 -*-
#
#    BitcoinLib - Python Cryptocurrency Library
#    WALLETS - HD wallet Class for key and transaction management
#    © 2017 November - 1200 Web Development <http://1200wd.com/>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import numbers
from copy import deepcopy
import struct
from sqlalchemy import or_
from bitcoinlib.db import *
from bitcoinlib.encoding import pubkeyhash_to_addr, to_hexstring, script_to_pubkeyhash
from bitcoinlib.keys import HDKey, check_network_and_key
from bitcoinlib.networks import Network, DEFAULT_NETWORK
from bitcoinlib.services.services import Service
from bitcoinlib.transactions import Transaction, serialize_multisig_redeemscript, Output, Input

_logger = logging.getLogger(__name__)


class WalletError(Exception):
    """
    Handle Wallet class Exceptions

    """
    def __init__(self, msg=''):
        self.msg = msg
        _logger.error(msg)

    def __str__(self):
        return self.msg


def wallets_list(databasefile=DEFAULT_DATABASE):
    """
    List Wallets from database
    
    :param databasefile: Location of Sqlite database. Leave empty to use default
    :type databasefile: str
    
    :return dict: Dictionary of wallets defined in database
    """

    session = DbInit(databasefile=databasefile).session
    wallets = session.query(DbWallet).all()
    wlst = []
    for w in wallets:
        wlst.append({
            'id': w.id,
            'name': w.name,
            'owner': w.owner,
            'network': w.network_name,
            'purpose': w.purpose,
        })
    session.close()
    return wlst


def wallet_exists(wallet, databasefile=DEFAULT_DATABASE):
    """
    Check if Wallets is defined in database
    
    :param wallet: Wallet ID as integer or Wallet Name as string
    :type wallet: int, str
    :param databasefile: Location of Sqlite database. Leave empty to use default
    :type databasefile: str
    
    :return bool: True if wallet exists otherwise False
    """

    if wallet in [x['name'] for x in wallets_list(databasefile)]:
        return True
    if isinstance(wallet, int) and wallet in [x['id'] for x in wallets_list(databasefile)]:
        return True
    return False


def wallet_create_or_open(name, key='', owner='', network=None, account_id=0, purpose=44, scheme='bip44',
                          parent_id=None, sort_keys=False, databasefile=DEFAULT_DATABASE):
    """
    Create a wallet with specified options if it doesn't exist, otherwise just open

    See Wallets class create method for option documentation

    """
    if wallet_exists(name, databasefile=databasefile):
        return HDWallet(name, databasefile=databasefile)
    else:
        return HDWallet.create(name, key, owner, network, account_id, purpose, scheme, parent_id, sort_keys,
                               databasefile)


def wallet_create_or_open_multisig(
        name, key_list, sigs_required=None, owner='', network=None, account_id=0,
        purpose=45, multisig_compressed=True, sort_keys=False, databasefile=DEFAULT_DATABASE):
    """
    Create a wallet with specified options if it doesn't exist, otherwise just open

    See Wallets class create method for option documentation

    """
    if wallet_exists(name, databasefile=databasefile):
        return HDWallet(name, databasefile=databasefile)
    else:
        return HDWallet.create_multisig(name, key_list, sigs_required, owner, network, account_id, purpose,
                                        multisig_compressed, sort_keys, databasefile)


def wallet_delete(wallet, databasefile=DEFAULT_DATABASE, force=False):
    """
    Delete wallet and associated keys from the database. If wallet has unspent outputs it raises a WalletError exception
    unless 'force=True' is specified
    
    :param wallet: Wallet ID as integer or Wallet Name as string
    :type wallet: int, str
    :param databasefile: Location of Sqlite database. Leave empty to use default
    :type databasefile: str
    :param force: If set to True wallet will be deleted even if unspent outputs are found. Default is False
    :type force: bool
    
    :return int: Number of rows deleted, so 1 if succesfull
    """

    session = DbInit(databasefile=databasefile).session
    if isinstance(wallet, int) or wallet.isdigit():
        w = session.query(DbWallet).filter_by(id=wallet)
    else:
        w = session.query(DbWallet).filter_by(name=wallet)
    if not w or not w.first():
        raise WalletError("Wallet '%s' not found" % wallet)
    wallet_id = w.first().id

    # Delete keys from this wallet and update transactions (remove key_id)
    ks = session.query(DbKey).filter_by(wallet_id=wallet_id)
    for k in ks:
        if not force and k.balance:
            raise WalletError("Key %d (%s) still has unspent outputs. Use 'force=True' to delete this wallet" %
                              (k.id, k.address))
        session.query(DbTransactionOutput).filter_by(key_id=k.id).update({DbTransactionOutput.key_id: None})
        session.query(DbTransactionInput).filter_by(key_id=k.id).update({DbTransactionInput.key_id: None})
        session.query(DbKeyMultisigChildren).filter_by(parent_id=k.id).delete()
        session.query(DbKeyMultisigChildren).filter_by(child_id=k.id).delete()
    ks.delete()

    res = w.delete()
    session.commit()
    session.close()

    # Delete co-signer wallets if this is a multisig wallet
    for cw in session.query(DbWallet).filter_by(parent_id=wallet_id).all():
        wallet_delete(cw.id, databasefile=databasefile, force=force)

    _logger.info("Wallet '%s' deleted" % wallet)

    return res


def wallet_delete_if_exists(wallet, databasefile=DEFAULT_DATABASE, force=False):
    if wallet_exists(wallet, databasefile):
        return wallet_delete(wallet, databasefile, force)


def normalize_path(path):
    """ Normalize BIP0044 key path for HD keys. Using single quotes for hardened keys 

    :param path: BIP0044 key path 
    :type path: str

    :return str: Normalized BIP0044 key path with single quotes
    """

    levels = path.split("/")
    npath = ""
    for level in levels:
        if not level:
            raise WalletError("Could not parse path. Index is empty.")
        nlevel = level
        if level[-1] in "'HhPp":
            nlevel = level[:-1] + "'"
        npath += nlevel + "/"
    if npath[-1] == "/":
        npath = npath[:-1]
    return npath


def parse_bip44_path(path):
    """
    Assumes a correct BIP0044 path and returns a dictionary with path items. See Bitcoin improvement proposals
    BIP0043 and BIP0044.
    
    Specify path in this format: m / purpose' / cointype' / account' / change / address_index.
    Path lenght must be between 1 and 6 (Depth between 0 and 5)
    
    :param path: BIP0044 path as string, with backslash (/) seperator. 
    :type path: str
    
    :return dict: Dictionary with path items: isprivate, purpose, cointype, account, change and address_index
    """

    pathl = normalize_path(path).split('/')
    if not 0 < len(pathl) <= 6:
        raise WalletError("Not a valid BIP0044 path. Path length (depth) must be between 1 and 6 not %d" % len(pathl))
    return {
        'isprivate': True if pathl[0] == 'm' else False,
        'purpose': '' if len(pathl) < 2 else pathl[1],
        'cointype': '' if len(pathl) < 3 else pathl[2],
        'account': '' if len(pathl) < 4 else pathl[3],
        'change': '' if len(pathl) < 5 else pathl[4],
        'address_index': '' if len(pathl) < 6 else pathl[5],
    }


class HDWalletKey:
    """
    Normally only used as attribute of HDWallet class. Contains HDKey object and extra information such as path and
    balance.
    
    All HDWalletKey are stored in a database
    """

    @staticmethod
    def from_key(name, wallet_id, session, key='',account_id=0, network=None, change=0,
                 purpose=44, parent_id=0, path='m', key_type=None):
        """
        Create HDWalletKey from a HDKey object or key
        
        :param name: New key name
        :type name: str
        :param wallet_id: ID of wallet where to store key
        :type wallet_id: int
        :param session: Required Sqlalchemy Session object
        :type session: sqlalchemy.orm.session.Session
        :param key: Optional key in any format accepted by the HDKey class
        :type key: str, int, byte, bytearray, HDKey
        :param account_id: Account ID for specified key, default is 0
        :type account_id: int
        :param network: Network of specified key
        :type network: str
        :param change: Use 0 for normal key, and 1 for change key (for returned payments)
        :type change: int
        :param purpose: BIP0044 purpose field, default is 44
        :type purpose: int
        :param parent_id: Key ID of parent, default is 0 (no parent)
        :type parent_id: int
        :param path: BIP0044 path of given key, default is 'm' (masterkey)
        :type path: str
        :param key_type: Type of key, single or BIP44 type
        :type key_type: str
        :return HDWalletKey: HDWalletKey object
        """

        if isinstance(key, HDKey):
            k = key
        else:
            if network is None:
                network = DEFAULT_NETWORK
            k = HDKey(import_key=key, network=network)

        keyexists = session.query(DbKey).filter(DbKey.wallet_id == wallet_id, DbKey.wif == k.wif()).first()
        if keyexists:
            _logger.warning("Key %s already exists" % (key or k.wif()))
            return HDWalletKey(keyexists.id, session, k)

        if key_type != 'single' and k.depth != len(path.split('/'))-1:
            if path == 'm' and k.depth == 3:
                # Create path when importing new account-key
                nw = Network(network)
                networkcode = nw.bip44_cointype
                path = "m/%d'/%s'/%d'" % (purpose, networkcode, account_id)
            else:
                raise WalletError("Key depth of %d does not match path lenght of %d for path %s" %
                                  (k.depth, len(path.split('/')) - 1, path))

        wk = session.query(DbKey).filter(DbKey.wallet_id == wallet_id,
                                         or_(DbKey.public == k.public_hex,
                                             DbKey.wif == k.wif())).first()
        if wk:
            return HDWalletKey(wk.id, session, k)

        nk = DbKey(name=name, wallet_id=wallet_id, public=k.public_hex, private=k.private_hex, purpose=purpose,
                   account_id=account_id, depth=k.depth, change=change, address_index=k.child_index,
                   wif=k.wif(), address=k.key.address(), parent_id=parent_id, compressed=k.compressed,
                   is_private=k.isprivate, path=path, key_type=key_type, network_name=network)
        session.add(nk)
        session.commit()
        return HDWalletKey(nk.id, session, k)

    def __init__(self, key_id, session, hdkey_object=None):
        """
        Initialize HDWalletKey with specified ID, get information from database.
        
        :param key_id: ID of key as mentioned in database
        :type key_id: int
        :param session: Required Sqlalchemy Session object
        :type session: sqlalchemy.orm.session.Session
        :param hdkey_object: Optional HDKey object 
        :type hdkey_object: HDKey
        """

        wk = session.query(DbKey).filter_by(id=key_id).first()
        if wk:
            self._dbkey = wk
            self._hdkey_object = hdkey_object
            self.key_id = key_id
            self.name = wk.name
            self.wallet_id = wk.wallet_id
            # self.key_hex = wk.key
            self.key_public = wk.public
            self.key_private = wk.private
            self.account_id = wk.account_id
            self.change = wk.change
            self.address_index = wk.address_index
            self.wif = wk.wif
            self.address = wk.address
            self._balance = wk.balance
            self.purpose = wk.purpose
            self.parent_id = wk.parent_id
            self.is_private = wk.is_private
            self.path = wk.path
            self.wallet = wk.wallet
            self.network_name = wk.network_name
            if not self.network_name:
                self.network_name = wk.wallet.network_name
            self.network = Network(self.network_name)
            self.depth = wk.depth
            self.key_type = wk.key_type
            self.compressed = wk.compressed
        else:
            raise WalletError("Key with id %s not found" % key_id)

    def __repr__(self):
        return "<HDWalletKey (name=%s, wif=%s, path=%s)>" % (self.name, self.wif, self.path)

    def key(self):
        """
        Get HDKey object for current HDWalletKey
        
        :return HDKey: 
        """

        if self._hdkey_object is None:
            self._hdkey_object = HDKey(import_key=self.wif, network=self.network_name)
        return self._hdkey_object

    def balance(self, fmt=''):
        """
        Get total of unspent outputs
        
        :param fmt: Specify 'string' to return a string in currency format
        :type fmt: str
        
        :return float, str: Key balance 
        """

        if fmt == 'string':
            return self.network.print_value(self._balance)
        else:
            return self._balance

    def fullpath(self, change=None, address_index=None, max_depth=5):
        """
        Full BIP0044 key path:
        - m / purpose' / coin_type' / account' / change / address_index
        
        :param change: Normal = 0, change =1
        :type change: int
        :param address_index: Index number of address (path depth 5)
        :type address_index: int
        :param max_depth: Maximum depth of output path. I.e. type 3 for account path
        :type max_depth: int
        
        :return list: Current key path 
        """

        if change is None:
            change = self.change
        if address_index is None:
            address_index = self.address_index
        if self.is_private:
            p = ["m"]
        else:
            p = ["M"]
        p.append(str(self.purpose) + "'")
        p.append(str(self.network.bip44_cointype) + "'")
        p.append(str(self.account_id) + "'")
        p.append(str(change))
        p.append(str(address_index))
        return p[:max_depth]

    # def info(self):
    #     """
    #     Output current key information to standard output
    #
    #     """
    #
    #     print("--- Key ---")
    #     print(" ID                             %s" % self.key_id)
    #     print(" Key Type                       %s" % self.key_type)
    #     print(" Is Private                     %s" % self.is_private)
    #     print(" Name                           %s" % self.name)
    #     if self.is_private:
    #         print(" Private Key                    %s" % self.key_private)
    #     print(" Public Key                     %s" % self.key_public)
    #     print(" Key WIF                        %s" % self.wif)
    #     print(" Account ID                     %s" % self.account_id)
    #     print(" Parent ID                      %s" % self.parent_id)
    #     print(" Depth                          %s" % self.depth)
    #     print(" Change                         %s" % self.change)
    #     print(" Address Index                  %s" % self.address_index)
    #     print(" Address                        %s" % self.address)
    #     print(" Path                           %s" % self.path)
    #     print(" Balance                        %s" % self.balance(fmt='string'))
    #     print("\n")

    def dict(self):
        """
        Return current key information as dictionary

        """

        return {
            'id': self.key_id,
            'key_type': self.key_type,
            'is_private': self.is_private,
            'name': self.name,
            'key_private': self.key_private,
            'key_public': self.key_public,
            'wif': self.wif,
            'account_id':  self.account_id,
            'parent_id': self.parent_id,
            'depth': self.depth,
            'change': self.change,
            'address_index': self.address_index,
            'address': self.address,
            'path': self.path,
            'balance': self.balance(),
            'balance_str': self.balance(fmt='string')
        }


class HDWallet:
    """
    Class to create and manage keys Using the BIP0044 Hierarchical Deterministic wallet definitions, so you can 
    use one Masterkey to generate as much child keys as you want in a structured manner.
    
    You can import keys in many format such as WIF or extended WIF, bytes, hexstring, seeds or private key integer.
    For the Bitcoin network, Litecoin or any other network you define in the settings.
    
    Easily send and receive transactions. Compose transactions automatically or select unspent outputs.
    
    Each wallet name must be unique and can contain only one cointype and purpose, but practically unlimited
    accounts and addresses. 
    """

    @classmethod
    def create(cls, name, key='', owner='', network=None, account_id=0, purpose=44, scheme='bip44', parent_id=None,
               sort_keys=False, databasefile=None):
        """
        Create HDWallet and insert in database. Generate masterkey or import key when specified. 
        
        Please mention account_id if you are using multiple accounts.
        
        :param name: Unique name of this Wallet
        :type name: str
        :param key: Masterkey to use for this wallet. Will be automatically created if not specified
        :type key: str, bytes, int, bytearray
        :param owner: Wallet owner for your own reference
        :type owner: str
        :param network: Network name, use default if not specified
        :type network: str
        :param account_id: Account ID, default is 0
        :type account_id: int
        :param purpose: BIP44 purpose field, default is 44
        :type purpose: int
        :param scheme: Key structure type, i.e. BIP44, single or multisig
        :type scheme: str
        :param parent_id: Parent Wallet ID used for multisig wallet structures
        :type parent_id: int
        :param sort_keys: Sort keys according to BIP45 standard (used for multisig keys)
        :type sort_keys: bool
        :param databasefile: Location of database file. Leave empty to use default
        :type databasefile: str
        
        :return HDWallet: 
        """

        if databasefile is None:
            databasefile = DEFAULT_DATABASE
        session = DbInit(databasefile=databasefile).session
        if session.query(DbWallet).filter_by(name=name).count():
            raise WalletError("Wallet with name '%s' already exists" % name)
        else:
            _logger.info("Create new wallet '%s'" % name)
        if isinstance(key, HDKey):
            network = key.network.network_name
        elif key:
            network = check_network_and_key(key, network)
            key = HDKey(key, network=network)
            # searchkey = session.query(DbKey).filter_by(wif=key).scalar()
            # if searchkey:
            #     raise WalletError("Key already found in wallet %s" % searchkey.wallet.name)
        elif network is None:
            network = DEFAULT_NETWORK
        new_wallet = DbWallet(name=name, owner=owner, network_name=network, purpose=purpose, scheme=scheme,
                              sort_keys=sort_keys, parent_id=parent_id)
        session.add(new_wallet)
        session.commit()
        new_wallet_id = new_wallet.id

        if scheme == 'bip44':
            mk = HDWalletKey.from_key(key=key, name=name, session=session, wallet_id=new_wallet_id, network=network,
                                      account_id=account_id, purpose=purpose, key_type='bip32')
            if mk.depth > 4:
                raise WalletError("Cannot create new wallet with main key of depth 5 or more")
            new_wallet.main_key_id = mk.key_id
            session.commit()

            w = cls(new_wallet_id, databasefile=databasefile, main_key_object=mk.key())
            if mk.depth == 0:
                nw = Network(network)
                networkcode = nw.bip44_cointype
                path = ["%d'" % purpose, "%s'" % networkcode]
                w._create_keys_from_path(mk, path, name=name, wallet_id=new_wallet_id, network=network, session=session,
                                         account_id=account_id, purpose=purpose, basepath="m")
                w.new_account(account_id=account_id)
        elif scheme == 'multisig':
            w = cls(new_wallet_id, databasefile=databasefile)
        elif scheme == 'single':
            mk = HDWalletKey.from_key(key=key, name=name, session=session, wallet_id=new_wallet_id, network=network,
                                      account_id=account_id, purpose=purpose, key_type='single')
            new_wallet.main_key_id = mk.key_id
            session.commit()
            w = cls(new_wallet_id, databasefile=databasefile, main_key_object=mk.key())
        else:
            raise WalletError("Wallet with scheme %s not supported at the moment" % scheme)

        session.close()
        return w

    @classmethod
    def create_multisig(cls, name, key_list, sigs_required=None, owner='', network=None, account_id=0, purpose=45,
                        multisig_compressed=True, sort_keys=False, databasefile=None):
        """
        Create a multisig wallet with specified name and list of keys. The list of keys can contain 2 or more
        public or private keys. For every key a cosigner wallet will be created with a BIP44 key structure or a
        single key depending on the key_type.

        :param name: Unique name of this Wallet
        :type name: str
        :param key_list: List of keys in HDKey format or any other format supported by HDKey class
        :type key_list: list
        :param sigs_required: Number of signatures required for validation. For example 2 for 2-of-3 multisignature. Default is all keys must signed
        :type sigs_required: int
        :type owner: str
        :param network: Network name, use default if not specified
        :type network: str
        :param account_id: Account ID, default is 0
        :type account_id: int
        :param purpose: BIP44 purpose field, default is 44
        :type purpose: int
        :param sort_keys: Sort keys according to BIP45 standard (used for multisig keys)
        :type sort_keys: bool
        :param databasefile: Location of database file. Leave empty to use default
        :type databasefile: str

        :return HDWallet:

        """
        if databasefile is None:
            databasefile = DEFAULT_DATABASE
        session = DbInit(databasefile=databasefile).session
        if session.query(DbWallet).filter_by(name=name).count():
            raise WalletError("Wallet with name '%s' already exists" % name)
        else:
            _logger.info("Create new multisig wallet '%s'" % name)
        if not isinstance(key_list, list):
            raise WalletError("Need list of keys to create multi-signature key structure")
        if len(key_list) < 2:
            raise WalletError("Key list must contain at least 2 keys")
        if sigs_required is None:
            sigs_required = len(key_list)
        if sigs_required > len(key_list):
            raise WalletError("Number of keys required to sign is greater then number of keys provided")

        hdpm = cls.create(name=name, owner=owner, network=network, account_id=account_id,
                          purpose=purpose, scheme='multisig', sort_keys=sort_keys, databasefile=databasefile)
        hdpm.multisig_compressed = multisig_compressed
        co_id = 0
        hdpm.cosigner = []
        hdkey_list = []
        for cokey in key_list:
            if not isinstance(cokey, HDKey):
                hdkey_list.append(HDKey(cokey))
            else:
                hdkey_list.append(cokey)
        if sort_keys:
            hdkey_list.sort(key=lambda x: x.public_byte)
        # TODO: Allow HDKey objects in Wallet.create (?)
        # key_wif_list2 = [k.wif() for k in hdkey_list]
        for cokey in hdkey_list:
            if hdpm.network.network_name != cokey.network.network_name:
                raise WalletError("Network for key %s (%s) is different then network specified: %s/%s" %
                                  (cokey.wif(), cokey.network.network_name, network, hdpm.network.network_name))
            scheme = 'bip44'
            wn = name + '-cosigner-%d' % co_id
            if cokey.key_type == 'single':
                scheme = 'single'
            w = cls.create(name=wn, key=cokey.wif(), owner=owner, network=network, account_id=account_id,
                           purpose=purpose, parent_id=hdpm.wallet_id, databasefile=databasefile, scheme=scheme)
            hdpm.cosigner.append(w)
            co_id += 1

        hdpm.multisig_n_required = sigs_required
        hdpm.sort_keys = sort_keys
        session.query(DbWallet).filter(DbWallet.id == hdpm.wallet_id).\
            update({DbWallet.multisig_n_required: sigs_required})
        session.commit()
        session.close()
        return hdpm

    def _create_keys_from_path(self, parent, path, wallet_id, account_id, network, session,
                               name='', basepath='', change=0, purpose=44):
        """
        Create all keys for a given path.
        
        :param parent: Main parent key. Can be a BIP0044 master key, level 3 account key, or any other.
        :type parent: HDWalletKey
        :param path: Path of keys to generate, relative to given parent key
        :type path: list
        :param wallet_id: Wallet ID
        :type wallet_id: int
        :param account_id: Account ID
        :type account_id: int
        :param network: Network
        :type network: str
        :param session: Sqlalchemy session
        :type session: sqlalchemy.orm.session.Session
        :param name: Name for generated keys. Leave empty for default
        :type name: str
        :param basepath: Basepath of main parent key
        :type basepath: str
        :param change: Change = 1, or payment = 0. Default is 0.
        :type change: int
        :param purpose: Purpose field according to BIP32 definition, default is 44 for BIP44.
        :type purpose: int
        
        :return HDWalletKey: 
        """

        # Initial checks and settings
        if not isinstance(parent, HDWalletKey):
            raise WalletError("Parent must be of type 'HDWalletKey'")
        if not isinstance(path, list):
            raise WalletError("Path must be of type 'list'")
        if len(basepath) and basepath[-1] != "/":
            basepath += "/"
        nk = parent
        ck = nk.key()

        # Check for closest ancestor in wallet
        spath = basepath + '/'.join(path)
        rkey = None
        while spath and not rkey:
            rkey = self._session.query(DbKey).filter_by(wallet_id=wallet_id, path=spath).first()
            spath = '/'.join(spath.split("/")[:-1])
        if rkey is not None and rkey.path not in [basepath, basepath[:-1]]:
            path = (basepath + '/'.join(path)).replace(rkey.path + '/', '').split('/')
            basepath = rkey.path + '/'
            nk = self.key(rkey.id)
            ck = nk.key()

        parent_id = nk.key_id
        # Create new keys from path
        for l in range(len(path)):
            pp = "/".join(path[:l+1])
            fullpath = basepath + pp
            ck = ck.subkey_for_path(path[l], network=network)
            nk = HDWalletKey.from_key(key=ck, name=name, wallet_id=wallet_id, network=network,
                                      account_id=account_id, change=change, purpose=purpose, path=fullpath,
                                      parent_id=parent_id, session=session)
            self._key_objects.update({nk.key_id: nk})
            parent_id = nk.key_id
        _logger.info("New key(s) created for parent_id %d" % parent_id)
        return nk

    def __enter__(self):
        return self

    def __init__(self, wallet, databasefile=DEFAULT_DATABASE, session=None, main_key_object=None):
        """
        Open a wallet with given ID or name
        
        :param wallet: Wallet name or ID
        :type wallet: int, str
        :param databasefile: Location of database file. Leave empty to use default
        :type databasefile: str
        :param main_key_object: Pass main key object to save time
        :type main_key_object: HDKey
        """

        if session:
            self._session = session
        else:
            self._session = DbInit(databasefile=databasefile).session
        if isinstance(wallet, int) or wallet.isdigit():
            w = self._session.query(DbWallet).filter_by(id=wallet).scalar()
        else:
            w = self._session.query(DbWallet).filter_by(name=wallet).scalar()
        if w:
            self._dbwallet = w
            self.wallet_id = w.id
            self._name = w.name
            self._owner = w.owner
            self.network = Network(w.network_name)
            self.purpose = w.purpose
            self.scheme = w.scheme
            self._balance = None
            self._balances = {}
            self.main_key_id = w.main_key_id
            self.main_key = None
            self.default_account_id = 0
            self.multisig_n_required = w.multisig_n_required
            self.multisig_compressed = None
            co_sign_wallets = self._session.query(DbWallet).\
                filter(DbWallet.parent_id == self.wallet_id).order_by(DbWallet.name).all()
            self.cosigner = [HDWallet(w.id) for w in co_sign_wallets]
            self.sort_keys = w.sort_keys
            if main_key_object:
                self.main_key = HDWalletKey(self.main_key_id, session=self._session, hdkey_object=main_key_object)
            elif w.main_key_id:
                self.main_key = HDWalletKey(self.main_key_id, session=self._session)
            if self.main_key:
                self.default_account_id = self.main_key.account_id
            _logger.info("Opening wallet '%s'" % self.name)
            self._key_objects = {
                self.main_key_id: self.main_key
            }
        else:
            raise WalletError("Wallet '%s' not found, please specify correct wallet ID or name." % wallet)

    def __exit__(self, exception_type, exception_value, traceback):
        self._session.close()

    def __repr__(self):
        return "<HDWallet (id=%d, name=%s, default_network=%s)>" % \
               (self.wallet_id, self.name, self.network.network_name)

    def _get_account_defaults(self, network=None, account_id=None):
        """
        Check parameter values for network and account ID, return defaults if no network or account ID is specified.
        If a network is specified but no account ID this method returns the first account ID it finds. 
        
        :param network: Network code, leave empty for default
        :type network: str
        :param account_id: Account ID, leave emtpy for default
        :type account_id: int
        
        :return: network code, account ID and DbKey instance of account ID key
        """

        if network is None:
            network = self.network.network_name
            if account_id is None:
                account_id = self.default_account_id
        qr = self._session.query(DbKey).\
            filter_by(wallet_id=self.wallet_id, purpose=self.purpose, depth=3, network_name=network)
        if account_id is not None:
            qr = qr.filter_by(account_id=account_id)
        acckey = qr.first()
        if len(qr.all()) > 1:
            _logger.warning("No account_id specified and more than one account found for this network %s. "
                            "Using a random account" % network)
        if not account_id and acckey:
            account_id = acckey.account_id
        return network, account_id, acckey

    @property
    def owner(self):
        """
        Get wallet Owner
        
        :return str: 
        """

        return self._owner

    @owner.setter
    def owner(self, value):
        """
        Set wallet Owner in database
        
        :param value: Owner
        :type value: str
        
        :return str: 
        """

        self._owner = value
        self._dbwallet.owner = value
        self._session.commit()

    @property
    def name(self):
        """
        Get wallet name
        
        :return str: 
        """

        return self._name

    @name.setter
    def name(self, value):
        """
        Set wallet name, update in database
        
        :param value: Name for this wallet
        :type value: str
        
        :return str: 
        """

        if wallet_exists(value):
            raise WalletError("Wallet with name '%s' already exists" % value)
        self._name = value
        self._dbwallet.name = value
        self._session.commit()

    def key_add_private(self, wallet_key, private_key):
        """
        Change public key in wallet to private key in current HDWallet object and in database

        :param wallet_key: Key object of wallet
        :type wallet_key: HDWalletKey
        :param private_key: Private key wif or HDKey object
        :type private_key: HDKey, str

        :return HDWalletKey:
        """
        assert isinstance(wallet_key, HDWalletKey)
        if not isinstance(private_key, HDKey):
            private_key = HDKey(private_key)
        wallet_key.is_private = True
        wallet_key.wif = private_key.wif()
        wallet_key.private = private_key.private_hex
        self._session.query(DbKey).filter(DbKey.id == wallet_key.key_id).update(
                {DbKey.is_private: True, DbKey.private: private_key.private_hex, DbKey.wif: private_key.wif()})
        self._session.commit()
        return wallet_key

    def import_master_key(self, hdkey, name='Masterkey (imported)'):
        network, account_id, acckey = self._get_account_defaults()

        if not isinstance(hdkey, HDKey):
            hdkey = HDKey(hdkey)
        if not isinstance(self.main_key, HDWalletKey):
            raise WalletError("Main wallet key is not an HDWalletKey instance. Type %s" % type(self.main_key))
        if not hdkey.isprivate or hdkey.depth != 0:
            raise WalletError("Please supply a valid private BIP32 master key with key depth 0")
        if self.main_key.depth != 3 or self.main_key.is_private or self.main_key.key_type != 'bip32':
            raise WalletError("Current main key is not a valid BIP32 public account key")
        if self.main_key.wif != hdkey.account_key().wif_public():
            raise WalletError("This key does not correspond to current main account key")
        if not (self.network.network_name == self.main_key.network.network_name == hdkey.network.network_name):
            raise WalletError("Network of Wallet class, main account key and the imported private key must use "
                              "the same network")

        self.main_key = HDWalletKey.from_key(
            key=hdkey.wif(), name=name, session=self._session, wallet_id=self.wallet_id, network=network,
            account_id=account_id, purpose=self.purpose, key_type='bip32')
        self.main_key_id = self.main_key.key_id
        network_code = self.network.bip44_cointype
        path = ["%d'" % self.purpose, "%s'" % network_code]
        self._create_keys_from_path(
            self.main_key, path, name=name, wallet_id=self.wallet_id, network=network, session=self._session,
            account_id=account_id, purpose=self.purpose, basepath="m")

        self._key_objects = {
            self.main_key_id: self.main_key
        }
        # FIXME: Use wallet object for this (integrate self._db and self)
        self._session.query(DbWallet).filter(DbWallet.id == self.wallet_id).\
            update({DbWallet.main_key_id: self.main_key_id})
        self._session.commit()
        return self.main_key

    def import_key(self, key, account_id=0, name='', network=None, purpose=44, key_type=None):
        """
        Add new single key to wallet.
        
        :param key: Key to import
        :type key: str, bytes, int, bytearray
        :param account_id: Account ID. Default is last used or created account ID.
        :type account_id: int
        :param name: Specify name for key, leave empty for default
        :type name: str
        :param network: Network name, method will try to extract from key if not specified. Raises warning if network could not be detected
        :type network: str
        :param purpose: BIP definition used, default is BIP44
        :type purpose: int
        :param key_type: Key type of imported key, can be single (unrelated to wallet, bip32, bip44 or master for new or extra master key import. Default is 'single'
        :type key_type: str
        
        :return HDWalletKey: 
        """

        if isinstance(key, HDKey):
            network = key.network.network_name
            hdkey = key
        else:
            if network is None:
                network = check_network_and_key(key, default_network=self.network.network_name)
                if network not in self.network_list():
                    raise WalletError("Network %s not available in this wallet, please create an account for this "
                                      "network first." % network)

            hdkey = HDKey(key, network=network, key_type=key_type)

        # TODO: Add multisig BIP45 support
        if self.main_key and self.main_key.depth == 3 and \
                hdkey.isprivate and hdkey.depth == 0 and self.scheme == 'bip44':
            hdkey.key_type = 'bip32'
            return self.import_master_key(hdkey, name)

        if key_type is None:
            hdkey.key_type = 'single'
            key_type = 'single'

        ik_path = 'm'
        if key_type == 'single':
            # Create path for unrelated import keys
            last_import_key = self._session.query(DbKey).filter(DbKey.path.like("import_key_%")).\
                order_by(DbKey.path.desc()).first()
            if last_import_key:
                ik_path = "import_key_" + str(int(last_import_key.path[-5:]) + 1).zfill(5)
            else:
                ik_path = "import_key_00001"
            if not name:
                name = ik_path

        mk = HDWalletKey.from_key(
            key=hdkey, name=name, wallet_id=self.wallet_id, network=network, key_type=key_type,
            account_id=account_id, purpose=purpose, session=self._session, path=ik_path)
        return mk

    def new_key(self, name='', account_id=None, network=None, change=0, max_depth=5):
        """
        Create a new HD Key derived from this wallet's masterkey. An account will be created for this wallet
        with index 0 if there is no account defined yet.
        
        :param name: Key name. Does not have to be unique but if you use it at reference you might chooce to enforce this. If not specified 'Key #' with an unique sequence number will be used
        :type name: str
        :param account_id: Account ID. Default is last used or created account ID.
        :type account_id: int
        :param network: Network name. Leave empty for default network
        :type network: str
        :param change: Change (1) or payments (0). Default is 0
        :type change: int
        :param max_depth: Maximum path depth. Default for BIP0044 is 5, any other value is non-standard and might cause unexpected behavior
        :type max_depth: int
        
        :return HDWalletKey: 
        """

        if self.scheme == 'single':
            return self.main_key

        network, account_id, acckey = self._get_account_defaults(network, account_id)
        if self.scheme == 'bip44':
            # Get account key, create one if it doesn't exist
            if not acckey:
                acckey = self._session.query(DbKey). \
                    filter_by(wallet_id=self.wallet_id, purpose=self.purpose, account_id=account_id,
                              depth=3, network_name=network).scalar()
            if not acckey:
                hk = self.new_account(account_id=account_id, network=network)
                if hk:
                    acckey = hk._dbkey
            if not acckey:
                raise WalletError("No key found this wallet_id, network and purpose. "
                                  "Is there a master key imported?")
            else:
                main_acc_key = self.key(acckey.id)

            # Determine new key ID
            prevkey = self._session.query(DbKey). \
                filter_by(wallet_id=self.wallet_id, purpose=self.purpose, network_name=network,
                          account_id=account_id, change=change, depth=max_depth). \
                order_by(DbKey.address_index.desc()).first()
            address_index = 0
            if prevkey:
                address_index = prevkey.address_index + 1

            # Compose key path and create new key
            newpath = [(str(change)), str(address_index)]
            bpath = main_acc_key.path + '/'
            if not name:
                if change:
                    name = "Change %d" % address_index
                else:
                    name = "Key %d" % address_index
            newkey = self._create_keys_from_path(
                main_acc_key, newpath, name=name, wallet_id=self.wallet_id,  account_id=account_id,
                change=change, network=network, purpose=self.purpose, basepath=bpath, session=self._session
            )
            return newkey
        elif self.scheme == 'multisig':
            if self.network.network_name != network:
                raise WalletError("Multiple networks is currently not supported for multisig")
                # TODO: Should be quite easy to support this...
            if not self.multisig_n_required:
                raise WalletError("Multisig_n_required not set, cannot create new key")
            co_sign_wallets = self._session.query(DbWallet).\
                filter(DbWallet.parent_id == self.wallet_id).order_by(DbWallet.name).all()

            public_keys = []
            for csw in co_sign_wallets:
                w = HDWallet(csw.id, session=self._session)
                wk = w.new_key(change=change, max_depth=max_depth, network=network)
                public_keys.append({
                    'key_id': wk.key_id,
                    'public_key_uncompressed': wk.key().key.public_uncompressed(),
                    'public_key': wk.key().key.public()
                })
            if self.sort_keys:
                public_keys.sort(key=lambda x: x['public_key'])
            public_key_list = [x['public_key'] for x in public_keys]
            public_key_ids = [str(x['key_id']) for x in public_keys]

            # Calculate redeemscript and address and add multisig key to database
            redeemscript = serialize_multisig_redeemscript(public_key_list, n_required=self.multisig_n_required)
            address = pubkeyhash_to_addr(script_to_pubkeyhash(redeemscript),
                                         versionbyte=Network(network).prefix_address_p2sh)
            path = "multisig-%d-of-" % self.multisig_n_required + '/'.join(public_key_ids)
            if not name:
                name = "Multisig Key " + '/'.join(public_key_ids)
            multisig_key = DbKey(
                name=name, wallet_id=self.wallet_id, purpose=self.purpose, account_id=account_id,
                depth=0, change=change, address_index=0, parent_id=0, is_private=False, path=path,
                public=to_hexstring(redeemscript), wif='multisig-%s' % address, address=address,
                key_type='multisig', network_name=network)
            self._session.add(multisig_key)
            self._session.commit()
            for child_id in public_key_ids:
                self._session.add(DbKeyMultisigChildren(key_order=public_key_ids.index(child_id),
                                                        parent_id=multisig_key.id, child_id=child_id))
            self._session.commit()
            return HDWalletKey(multisig_key.id, session=self._session)

    def new_key_change(self, name='', account_id=None, network=None):
        """
        Create new key to receive change for a transaction. Calls new_key method with change=1.
        
        :param name: Key name. Default name is 'Change #' with an address index
        :type name: str
        :param account_id: Account ID. Default is last used or created account ID.
        :type account_id: int
        :param network: Network name. Leave empty for default network
        :type network: str
                
        :return HDWalletKey: 
        """

        return self.new_key(name=name, account_id=account_id, network=network, change=1)

    def scan(self, scan_depth=10, account_id=None, change=None, network=None, _recursion_depth=0):
        """
        Generate new keys for this wallet and scan for UTXO's

        :param scan_depth: Amount of new keys and change keys (addresses) created for this wallet
        :type scan_depth: int
        :param account_id: Account ID. Default is last used or created account ID.
        :type account_id: int
        :param network: Network name. Leave empty for default network
        :type network: str

        :return:
        """

        if _recursion_depth > 10:
            raise WalletError("UTXO scanning has reached a recursion depth of more then 10")
        _recursion_depth += 1
        if self.scheme != 'bip44' and self.scheme != 'multisig':
            raise WalletError("The wallet scan() method is only available for BIP44 wallets")
        if change != 1:
            scanned_keys = self.get_key(account_id, network, number_of_keys=scan_depth)
            new_key_ids = [k.key_id for k in scanned_keys]
            nr_new_utxos = 0
            # TODO: Allow list of keys in utxos_update
            for new_key_id in new_key_ids:
                nr_new_utxos += self.utxos_update(change=0, key_id=new_key_id)
            if nr_new_utxos:
                self.scan(scan_depth, account_id, change=0, network=network, _recursion_depth=_recursion_depth)
        if change != 0:
            scanned_keys_change = self.get_key(account_id, network, change=1, number_of_keys=scan_depth)
            new_key_ids = [k.key_id for k in scanned_keys_change]
            nr_new_utxos = 0
            for new_key_id in new_key_ids:
                nr_new_utxos += self.utxos_update(change=1, key_id=new_key_id)
            if nr_new_utxos:
                self.scan(scan_depth, account_id, change=1, network=network, _recursion_depth=_recursion_depth)

    def get_key(self, account_id=None, network=None, number_of_keys=1, change=0, depth_of_keys=5):
        """
        Get a unused key or create a new one if there are no unused keys. 
        Returns a key from this wallet which has no transactions linked to it.
        
        :param account_id: Account ID. Default is last used or created account ID.
        :type account_id: int
        :param network: Network name. Leave empty for default network
        :type network: str
        :param change: Payment (0) or change key (1). Default is 0
        :type change: int
        :param depth_of_keys: Depth of account keys. Default is 5 according to BIP44 standards
        :type depth_of_keys: int
        
        :return HDWalletKey: 
        """

        network, account_id, _ = self._get_account_defaults(network, account_id)
        keys_depth = depth_of_keys
        if self.scheme == 'multisig':
            keys_depth = 0
        last_used_qr = self._session.query(DbKey).\
            filter_by(wallet_id=self.wallet_id, account_id=account_id, network_name=network,
                      used=True, change=change, depth=keys_depth).\
            order_by(DbKey.id.desc()).first()
        last_used_key_id = 0
        if last_used_qr:
            last_used_key_id = last_used_qr.id
        dbkey = self._session.query(DbKey).\
            filter_by(wallet_id=self.wallet_id, account_id=account_id, network_name=network,
                      used=False, change=change, depth=keys_depth).filter(DbKey.id > last_used_key_id).\
            order_by(DbKey.id).all()
        key_list = []
        for i in range(number_of_keys):
            if dbkey:
                dk = dbkey.pop()
                nk = HDWalletKey(dk.id, session=self._session)
            else:
                nk = self.new_key(account_id=account_id, network=network, change=change, max_depth=depth_of_keys)
            key_list.append(nk)
        if len(key_list) == 1:
            return key_list[0]
        else:
            return key_list

    def get_keys(self, account_id=None, network=None, change=0, depth_of_keys=5):
        """
        Get a unused key or create a new one if there are no unused keys.
        Returns a key from this wallet which has no transactions linked to it.

        :param account_id: Account ID. Default is last used or created account ID.
        :type account_id: int
        :param network: Network name. Leave empty for default network
        :type network: str
        :param change: Payment (0) or change key (1). Default is 0
        :type change: int
        :param depth_of_keys: Depth of account keys. Default is 5 according to BIP44 standards
        :type depth_of_keys: int

        :return HDWalletKey:
        """

        network, account_id, _ = self._get_account_defaults(network, account_id)
        keys_depth = depth_of_keys
        if self.scheme == 'multisig':
            keys_depth = 0
        dbkeys = self._session.query(DbKey). \
            filter_by(wallet_id=self.wallet_id, account_id=account_id, network_name=network,
                      used=False, change=change, depth=keys_depth). \
            order_by(DbKey.id).all()
        unusedkeys = []
        for dk in dbkeys:
            unusedkeys.append(HDWalletKey(dk.id, session=self._session))
        return unusedkeys

    def get_key_change(self, account_id=None, network=None, depth_of_keys=5):
        """
        Get a unused change key or create a new one if there are no unused keys. 
        Wrapper for the get_key method
        
        :param account_id: Account ID. Default is last used or created account ID.
        :type account_id: int
        :param network: Network name. Leave empty for default network
        :type network: str
        :param depth_of_keys: Depth of account keys. Default is 5 according to BIP44 standards
        :type depth_of_keys: int
        
        :return HDWalletKey:  
        """

        return self.get_key(account_id=account_id, network=network, change=1, depth_of_keys=depth_of_keys)

    def new_account(self, name='', account_id=None, network=None):
        """
        Create a new account with a childkey for payments and 1 for change.
        
        An account key can only be created if wallet contains a masterkey.
        
        :param name: Account Name. If not specified 'Account #" with the account_id will be used
        :type name: str
        :param account_id: Account ID. Default is last accounts ID + 1
        :type account_id: int
        :param network: Network name. Leave empty for default network
        :type network: str
        
        :return HDWalletKey: 
        """

        if self.scheme != 'bip44':
            raise WalletError("We can only create new accounts for a wallet with a BIP44 key scheme")
        if self.main_key.depth != 0 or self.main_key.is_private is False:
            raise WalletError("A master private key of depth 0 is needed to create new accounts (%s)" %
                              self.main_key.wif)

        if network is None:
            network = self.network.network_name

        # Determine account_id and name
        if account_id is None:
            account_id = 0
            qr = self._session.query(DbKey). \
                filter_by(wallet_id=self.wallet_id, purpose=self.purpose, network_name=network). \
                order_by(DbKey.account_id.desc()).first()
            if qr:
                account_id = qr.account_id + 1
        if not name:
            name = 'Account #%d' % account_id
        if self.keys(account_id=account_id, depth=3, network=network):
            raise WalletError("Account with ID %d already exists for this wallet")

        # Get root key of new account
        res = self.keys(depth=2, network=network)
        if not res:
            try:
                # TODO: make this better...
                purposekey = self.key(self.keys(depth=1)[0].id)
                bip44_cointype = Network(network).bip44_cointype
                accrootkey_obj = self._create_keys_from_path(
                    purposekey, ["%s'" % str(bip44_cointype)], name=network, wallet_id=self.wallet_id, account_id=account_id,
                    network=network, purpose=self.purpose, basepath=purposekey.path,
                    session=self._session)
            except IndexError:
                raise WalletError("No key found for this wallet_id and purpose. Can not create new"
                                  "account for this wallet, is there a master key imported?")
        else:
            accrootkey = res[0]
            accrootkey_obj = self.key(accrootkey.id)

        # Create new account addresses and return main account key
        newpath = [str(account_id) + "'"]
        acckey = self._create_keys_from_path(
            accrootkey_obj, newpath, name=name, wallet_id=self.wallet_id,  account_id=account_id,
            network=network, purpose=self.purpose, basepath=accrootkey_obj.path, session=self._session
        )
        self._create_keys_from_path(
            acckey, ['0'], name=acckey.name + ' Payments', wallet_id=self.wallet_id, account_id=account_id,
            network=network, purpose=self.purpose, basepath=acckey.path,  session=self._session)
        self._create_keys_from_path(
            acckey, ['1'], name=acckey.name + ' Change', wallet_id=self.wallet_id, account_id=account_id,
            network=network, purpose=self.purpose, basepath=acckey.path, session=self._session)
        return acckey

    def key_for_path(self, path, name='', account_id=0, change=0, enable_checks=True):
        """
        Create key with specified path. Can be used to create non-default (non-BIP0044) paths.
        
        Can cause problems if already used account ID's or address indexes are provided.
        
        :param path: Path string in m/#/#/# format. With quote (') or (p/P/h/H) character for hardened child key derivation
        :type path: str
        :param name: Key name to use
        :type name: str
        :param account_id: Account ID
        :type account_id: int
        :param change: Change 0 or 1
        :type change: int
        :param enable_checks: Use checks for valid BIP0044 path, default is True
        :type enable_checks: bool
        
        :return HDWalletKey: 
        """

        # Validate key path
        if path not in ['m', 'M'] and enable_checks:
            pathdict = parse_bip44_path(path)
            purpose = 0 if not pathdict['purpose'] else int(pathdict['purpose'].replace("'", ""))
            if purpose != self.purpose:
                raise WalletError("Cannot create key with different purpose field (%d) as existing wallet (%d)" %
                                  (purpose, self.purpose))
            cointype = int(pathdict['cointype'].replace("'", ""))
            wallet_cointypes = [Network(nw).bip44_cointype for nw in self.network_list()]
            if cointype not in wallet_cointypes:
                raise WalletError("Network / cointype %s not available in this wallet, please create an account for "
                                  "this network first. Or disable BIP checks." % cointype)
            if pathdict['cointype'][-1] != "'" or pathdict['purpose'][-1] != "'" or pathdict['account'][-1] != "'":
                raise WalletError("Cointype, purpose and account must be hardened, see BIP43 and BIP44 definitions")
        if not name:
            name = self.name

        # Check for closest ancestor in wallet
        spath = normalize_path(path)
        rkey = None
        while spath and not rkey:
            rkey = self._session.query(DbKey).filter_by(path=spath, wallet_id=self.wallet_id).first()
            spath = '/'.join(spath.split("/")[:-1])

        # Key already found in db, return key
        if rkey and rkey.path == path:
            return self.key(rkey.id)

        parent_key = self.main_key
        subpath = path
        basepath = ''
        if rkey is not None:
            subpath = normalize_path(path).replace(rkey.path + '/', '')
            basepath = rkey.path
            if self.main_key.wif != rkey.wif:
                parent_key = self.key(rkey.id)
        newkey = self._create_keys_from_path(
            parent_key, subpath.split("/"), name=name, wallet_id=self.wallet_id,
            account_id=account_id, change=change,
            network=self.network.network_name, purpose=self.purpose, basepath=basepath, session=self._session)
        return newkey

    def keys(self, account_id=None, name=None, key_id=None, change=None, depth=None, used=None, is_private=None,
             has_balance=None, is_active=True, network=None, as_dict=False):
        """
        Search for keys in database. Include 0 or more of account_id, name, key_id, change and depth.
        
        Returns a list of DbKey object or dictionary object if as_dict is True
        
        :param account_id: Search for account ID 
        :type account_id: int
        :param name: Search for Name
        :type name: str
        :param key_id: Search for Key ID
        :type key_id: int
        :param change: Search for Change
        :type change: int
        :param depth: Only include keys with this depth
        :type depth: int
        :param used: Only return used or unused keys
        :type used: bool
        :param is_private: Only return private keys
        :type is_private: bool
        :param has_balance: Only include keys with a balance or without a balance, default is both
        :type has_balance: bool
        :param is_active: Hide inactive keys. Only include active keys with either a balance or which are unused, default is True
        :type is_active: bool
        :param network: Network name filter
        :type network: str
        :param as_dict: Return keys as dictionary objects. Default is False: DbKey objects
        
        :return list: List of Keys
        """

        qr = self._session.query(DbKey).filter_by(wallet_id=self.wallet_id).order_by(DbKey.id)
        if network is not None:
            qr = qr.filter(DbKey.network_name == network)
        if account_id is not None:
            qr = qr.filter(DbKey.account_id == account_id)
            if self.scheme == 'bip44' and depth is None:
                qr = qr.filter(DbKey.depth >= 3)
        if change is not None:
            qr = qr.filter(DbKey.change == change)
            if self.scheme == 'bip44' and depth is None:
                qr = qr.filter(DbKey.depth > 4)
        if depth is not None:
            qr = qr.filter(DbKey.depth == depth)
        if name is not None:
            qr = qr.filter(DbKey.name == name)
        if key_id is not None:
            qr = qr.filter(DbKey.id == key_id)
        if used is not None:
            qr = qr.filter(DbKey.used == used)
        if is_private is not None:
            qr = qr.filter(DbKey.is_private == is_private)
        if has_balance is True and is_active is True:
            raise WalletError("Cannot use has_balance and hide_unused parameter together")
        if has_balance is not None:
            if has_balance:
                qr = qr.filter(DbKey.balance != 0)
            else:
                qr = qr.filter(DbKey.balance == 0)
        if is_active:  # Unused keys and keys with a balance
            qr = qr.filter(or_(DbKey.balance != 0, DbKey.used == False))
        ret = as_dict and [x.__dict__ for x in qr.all()] or qr.all()
        qr.session.close()
        return ret

    def keys_networks(self, used=None, as_dict=False):
        """
        Get keys of defined networks for this wallet. Wrapper for the keys() method

        :param used: Only return used or unused keys
        :type used: bool
        :param as_dict: Return as dictionary or DbKey object. Default is False: DbKey objects
        :type as_dict: bool
        
        :return list: DbKey or dictionaries
        
        """

        if self.scheme != 'bip44':
            raise WalletError("The 'keys_network' method can only be used with BIP44 type wallets")
        res = self.keys(depth=2, used=used, as_dict=as_dict)
        if not res:
            res = self.keys(depth=3, used=used, as_dict=as_dict)
        return res

    def keys_accounts(self, account_id=None, network=None, as_dict=False):
        """
        Get Database records of account key(s) with for current wallet. Wrapper for the keys() method.
        
        :param account_id: Search for Account ID
        :type account_id: int
        :param network: Network name filter
        :type network: str
        :param as_dict: Return as dictionary or DbKey object. Default is False: DbKey objects
        :type as_dict: bool
        
        :return list: DbKey or dictionaries
        """

        return self.keys(account_id, depth=3, network=network, as_dict=as_dict)

    def keys_addresses(self, account_id=None, used=None, network=None, as_dict=False):
        """
        Get address-keys of specified account_id for current wallet. Wrapper for the keys() methods.

        :param account_id: Account ID
        :type account_id: int
        :param used: Only return used or unused keys
        :type used: bool
        :param network: Network name filter
        :type network: str
        :param as_dict: Return as dictionary or DbKey object. Default is False: DbKey objects
        :type as_dict: bool
        
        :return list: DbKey or dictionaries
        """

        return self.keys(account_id, depth=5, used=used, network=network, as_dict=as_dict)

    def keys_address_payment(self, account_id=None, used=None, network=None, as_dict=False):
        """
        Get payment addresses (change=0) of specified account_id for current wallet. Wrapper for the keys() methods.

        :param account_id: Account ID
        :type account_id: int
        :param used: Only return used or unused keys
        :type used: bool
        :param network: Network name filter
        :type network: str
        :param as_dict: Return as dictionary or DbKey object. Default is False: DbKey objects
        :type as_dict: bool
        
        :return list: DbKey or dictionaries
        """

        return self.keys(account_id, depth=5, change=0, used=used, network=network, as_dict=as_dict)

    def keys_address_change(self, account_id=None, used=None, network=None, as_dict=False):
        """
        Get payment addresses (change=1) of specified account_id for current wallet. Wrapper for the keys() methods.

        :param account_id: Account ID
        :type account_id: int
        :param used: Only return used or unused keys
        :type used: bool
        :param network: Network name filter
        :type network: str
        :param as_dict: Return as dictionary or DbKey object. Default is False: DbKey objects
        :type as_dict: bool
        
        :return list: DbKey or dictionaries
        """

        return self.keys(account_id, depth=5, change=1, used=used, network=network, as_dict=as_dict)

    def addresslist(self, account_id=None, used=None, network=None, change=None, depth=5, key_id=None):
        """
        Get list of addresses defined in current wallet

        :param account_id: Account ID
        :type account_id: int
        :param used: Only return used or unused keys
        :type used: bool
        :param network: Network name filter
        :type network: str
        :param depth: Filter by key depth
        :type depth: int
        :param key_id: Key ID to get address of just 1 key
        :type key_id: int
        
        :return list: List of address strings
        """

        addresslist = []
        for key in self.keys(account_id=account_id, depth=depth, used=used, network=network, change=change,
                             key_id=key_id, is_active=False):
            addresslist.append(key.address)
        return addresslist

    def key(self, term):
        """
        Return single key with give ID or name as HDWalletKey object

        :param term: Search term can be key ID, key address, key WIF or key name
        :type term: int, str
        
        :return HDWalletKey: Single key as object
        """

        dbkey = None
        qr = self._session.query(DbKey).filter_by(wallet_id=self.wallet_id, purpose=self.purpose)
        if isinstance(term, numbers.Number):
            dbkey = qr.filter_by(id=term).scalar()
        if not dbkey:
            dbkey = qr.filter_by(address=term).first()
        if not dbkey:
            dbkey = qr.filter_by(wif=term).first()
        if not dbkey:
            dbkey = qr.filter_by(name=term).first()
        if dbkey:
            if dbkey.id in self._key_objects.keys():
                return self._key_objects[dbkey.id]
            else:
                return HDWalletKey(key_id=dbkey.id, session=self._session)
        else:
            raise KeyError("Key '%s' not found" % term)

    def account(self, account_id):
        """
        Returns wallet key of specific BIP44 account.

        Account keys have a BIP44 path depth of 3 and have the format m/purpose'/network'/account'

        I.e: Use account(0).key().wif_public() to get wallet's public account key

        :param account_id: ID of account. Default is 0
        :type account_id: int

        :return HDWalletKey:

        """
        qr = self._session.query(DbKey).\
            filter_by(wallet_id=self.wallet_id, purpose=self.purpose, network_name=self.network.network_name,
                      account_id=account_id, depth=3).scalar()
        if not qr:
            raise WalletError("Account with ID %d not found in this wallet" % account_id)
        key_id = qr.id
        return HDWalletKey(key_id, session=self._session)

    def accounts(self, network=None):
        """
        Get list of accounts for this wallet
        
        :param network: Network name filter
        :type network: str
                
        :return: List of keys as dictionary
        """

        wks = self.keys_accounts(network=network, as_dict=True)
        for wk in wks:
            if '_sa_instance_state' in wk:
                del wk['_sa_instance_state']
        return wks

    def networks(self):
        """
        Get list of networks used by this wallet
        
        :return: List of keys as dictionary
        """

        if self.scheme == 'bip44':
            wks = self.keys_networks(as_dict=True)
            for wk in wks:
                if '_sa_instance_state' in wk:
                    del wk['_sa_instance_state']
            return wks
        else:
            return [self.network.__dict__]

    def network_list(self, field='network_name'):
        """
        Wrapper for networks methods, returns a flat list with currently used
        networks for this wallet.
        
        :return: list 
        """

        return [x[field] for x in self.networks()]

    def balance_update_from_serviceprovider(self, account_id=None, network=None):
        """
        Update balance of currents account addresses using default Service objects getbalance method. Update total 
        wallet balance in database. 
        
        Please Note: Does not update UTXO's or the balance per key! For this use the 'updatebalance' method
        instead
        
        :param account_id: Account ID
        :type account_id: int
        :param network: Network name. Leave empty for default network
        :type network: str
        
        :return: 
        """

        network, account_id, acckey = self._get_account_defaults(network, account_id)
        balance = Service(network=network).getbalance(self.addresslist(account_id=account_id, network=network))
        self._balances.update({network: balance})
        self._dbwallet.balance = balance
        self._session.commit()

    def balance(self, network=None, as_string=False):
        """
        Get total of unspent outputs

        :param network: Network name. Leave empty for default network
        :type network: str
        :param as_string: Set True to return a string in currency format. Default returns float.
        :type as_string: boolean

        :return float, str: Key balance
        """

        if self._balance is None:
            self.balance_update()
        if network is None:
            network = self.network.network_name
        if network not in self._balances:
            return 0
        if as_string:
            return Network(network).print_value(self._balances[network])
        else:
            return self._balances[network]

    def balance_update(self, account_id=None, network=None, key_id=None, min_confirms=1):
        """
        Update balance from UTXO's in database. To get most recent balance update UTXO's first.
        
        Also updates balance of wallet and keys in this wallet for the specified account or all accounts if
        no account is specified.
        
        :param account_id: Account ID filter
        :type account_id: int
        :param network: Network name. Leave empty for default network
        :type network: str
        :param key_id: Key ID Filter
        :type key_id: int
        :param min_confirms: Minimal confirmations needed to include in balance (default = 1)
        :type min_confirms: int

        :return: Updated balance
        """

        qr = self._session.query(DbTransactionOutput, func.sum(DbTransactionOutput.value), DbKey.network_name).\
            join(DbTransaction).join(DbKey). \
            filter(DbTransactionOutput.spent.op("IS")(False),
                   DbTransaction.wallet_id == self.wallet_id,
                   DbTransaction.confirmations >= min_confirms)
        if account_id is not None:
            qr = qr.filter(DbKey.account_id == account_id)
        if network is not None:
            qr = qr.filter(DbKey.network_name == network)
        if key_id is not None:
            qr = qr.filter(DbKey.id == key_id)
        utxos = qr.group_by(DbTransactionOutput.key_id).all()
        key_values = []
        network_values = {}

        for utxo in utxos:
            key_values.append({
                'id': utxo[0].key_id,
                'balance': utxo[1]
            })
            network = utxo[2]
            new_value = utxo[1]
            if network in network_values:
                new_value = network_values[network] + utxo[0].value
            network_values.update({network: new_value})

        # Add keys with no UTXO's with 0 balance
        for key in self.keys(account_id=account_id, network=network, key_id=key_id):
            if key.id not in [utxo[0].key_id for utxo in utxos]:
                key_values.append({
                    'id': key.id,
                    'balance': 0
                })

        if not (key_id or account_id):
            self._balances.update(network_values)
            if self.network.network_name in network_values:
                self._balance = network_values[self.network.network_name]
        # TODO: else...

        # Bulk update database
        self._session.bulk_update_mappings(DbKey, key_values)
        self._session.commit()
        _logger.info("Got balance for %d key(s)" % len(key_values))
        return self._balance

    def utxos_update(self, account_id=None, used=None, network=None, key_id=None, depth=None, change=None, utxos=None):
        """
        Update UTXO's (Unspent Outputs) in database of given account using the default Service object.
        
        Delete old UTXO's which are spent and append new UTXO's to database.

        For usage on an offline PC, you can import utxos with the utxos parameter as a list of dictionaries:
        [{
            'address': 'n2S9Czehjvdmpwd2YqekxuUC1Tz5ZdK3YN',
            'script': '',
            'confirmations': 10,
            'output_n': 1,
            'tx_hash': '9df91f89a3eb4259ce04af66ad4caf3c9a297feea5e0b3bc506898b6728c5003',
            'value': 8970937
        }]

        :param account_id: Account ID
        :type account_id: int
        :param used: Only check for UTXO for used or unused keys. Default is both
        :type used: bool
        :param network: Network name. Leave empty for default network
        :type network: str
        :param key_id: Key ID to just update 1 key
        :type key_id: int
        :param depth: Only update keys with this depth, default is depth 5 according to BIP0048 standard. Set depth to None to update all keys of this wallet.
        :type depth: int
        :param change: Only update change or normal keys, default is both (None)
        :type change: int
        :param utxos: List of unspent outputs in dictionary format specified in this method DOC header
        :type utxos: list
        
        :return int: Number of new UTXO's added 
        """

        network, account_id, acckey = self._get_account_defaults(network, account_id)
        # TODO: implement bip45/67/electrum/?
        schemes_key_depth = {
            'bip44': 5,
            'single': 0,
            'electrum': 4,
            'multisig': 0
        }
        if depth is None:
            if self.scheme == 'bip44':
                depth = 5
            else:
                depth = 0

        if utxos is None:
            # Get all UTXO's for this wallet from default Service object
            addresslist = self.addresslist(account_id=account_id, used=used, network=network, key_id=key_id,
                                           change=change, depth=depth)
            utxos = Service(network=network).getutxos(addresslist)
            if utxos is False:
                raise WalletError("No response from any service provider, could not update UTXO's")
        count_utxos = 0

        # Get current UTXO's from database to compare with Service objects UTXO's
        current_utxos = self.utxos(account_id=account_id, network=network, key_id=key_id)

        # Update spent UTXO's (not found in list) and mark key as used
        utxos_tx_hashes = [(x['tx_hash'], x['output_n']) for x in utxos]
        for current_utxo in current_utxos:
            if (current_utxo['tx_hash'], current_utxo['output_n']) not in utxos_tx_hashes:
                utxo_in_db = self._session.query(DbTransactionOutput).join(DbTransaction). \
                    filter(DbTransaction.hash == current_utxo['tx_hash'],
                           DbTransactionOutput.output_n == current_utxo['output_n'])
                for utxo_record in utxo_in_db.all():
                    utxo_record.spent = True
            self._session.commit()

        # If UTXO is new, add to database otherwise update depth (confirmation count)
        for utxo in utxos:
            key = self._session.query(DbKey).filter_by(wallet_id=self.wallet_id, address=utxo['address']).scalar()
            if key and not key.used:
                key.used = True

            # Update confirmations in db if utxo was already imported
            # TODO: Add network filter (?)
            transaction_in_db = self._session.query(DbTransaction).filter_by(wallet_id=self.wallet_id,
                                                                             hash=utxo['tx_hash'])
            utxo_in_db = self._session.query(DbTransactionOutput).join(DbTransaction).\
                filter(DbTransaction.wallet_id == self.wallet_id,
                       DbTransaction.hash == utxo['tx_hash'],
                       DbTransactionOutput.output_n == utxo['output_n'])
            if utxo_in_db.count():
                utxo_record = utxo_in_db.scalar()
                if not utxo_record.key_id:
                    count_utxos += 1
                utxo_record.key_id = key.id
                utxo_record.spent = False
                transaction_record = transaction_in_db.scalar()
                transaction_record.confirmations = utxo['confirmations']
            else:
                # Add transaction if not exist and then add output
                if not transaction_in_db.count():
                    new_tx = DbTransaction(wallet_id=self.wallet_id, hash=utxo['tx_hash'],
                                           confirmations=utxo['confirmations'])
                    self._session.add(new_tx)
                    self._session.commit()
                    tid = new_tx.id
                else:
                    tid = transaction_in_db.scalar().id

                new_utxo = DbTransactionOutput(transaction_id=tid,  output_n=utxo['output_n'], value=utxo['value'],
                                               key_id=key.id, script=utxo['script'], spent=False)
                self._session.add(new_utxo)
                count_utxos += 1
            # TODO: Removing this gives errors??
            self._session.commit()

        _logger.info("Got %d new UTXOs for account %s" % (count_utxos, account_id))
        self._session.commit()
        self.balance_update(account_id=account_id, network=network, key_id=key_id, min_confirms=0)
        return count_utxos

    def _utxos_update_from_transactions(self, key_ids):
        for key_id in key_ids:
            outputs = self._session.query(DbTransactionOutput).filter_by(key_id=key_id).all()
            for to in outputs:
                if self._session.query(DbTransactionInput).\
                        filter_by(prev_hash=to.transaction.hash, input_n=to.output_n).scalar():
                    to.spent = True
        self._session.commit()

    def utxos(self, account_id=None, network=None, min_confirms=0, key_id=None):
        """
        Get UTXO's (Unspent Outputs) from database. Use utxos_update method first for updated values
        
        :param account_id: Account ID
        :type account_id: int
        :param network: Network name. Leave empty for default network
        :type network: str
        :param min_confirms: Minimal confirmation needed to include in output list
        :type min_confirms: int
        :param key_id: Key ID to just get 1 key
        :type key_id: int

        :return list: List of transactions 
        """

        network, account_id, acckey = self._get_account_defaults(network, account_id)

        qr = self._session.query(DbTransactionOutput, DbKey.address, DbTransaction.confirmations, DbTransaction.hash,
                                 DbKey.network_name).\
            join(DbTransaction).join(DbKey). \
            filter(DbTransactionOutput.spent.op("IS")(False),
                   DbKey.account_id == account_id,
                   DbTransaction.wallet_id == self.wallet_id,
                   DbKey.network_name == network,
                   DbTransaction.confirmations >= min_confirms)
        if key_id is not None:
            qr = qr.filter(DbKey.id == key_id)
        utxos = qr.order_by(DbTransaction.confirmations.desc()).all()
        res = []
        for utxo in utxos:
            u = utxo[0].__dict__
            if '_sa_instance_state' in u:
                del u['_sa_instance_state']
            u['address'] = utxo[1]
            u['confirmations'] = int(utxo[2])
            u['tx_hash'] = utxo[3]
            u['network_name'] = utxo[4]
            res.append(u)
        return res

    def transactions_update(self, account_id=None, used=None, network=None, key_id=None, depth=None, change=None):
        network, account_id, acckey = self._get_account_defaults(network, account_id)
        if depth is None:
            if self.scheme == 'bip44':
                depth = 5
            else:
                depth = 0
        addresslist = self.addresslist(account_id=account_id, used=used, network=network, key_id=key_id,
                                       change=change, depth=depth)
        srv = Service(network=network, providers=['bitgo'])
        txs = srv.gettransactions(addresslist)
        if txs is False:
            raise WalletError("No response from any service provider, could not update transactions")
        no_spent_info = False
        key_ids = set()
        for tx in txs:
            # If tx_hash is unknown add it to database, else update
            db_tx = self._session.query(DbTransaction).filter(DbTransaction.hash == tx['hash']).scalar()
            if not db_tx:
                new_tx = DbTransaction(wallet_id=self.wallet_id, hash=tx['hash'], block_height=tx['block_height'],
                                       confirmations=tx['confirmations'], date=tx['date'], fee=tx['fee'])
                self._session.add(new_tx)
                self._session.commit()
                tx_id = new_tx.id
            else:
                tx_id = db_tx.id

            assert tx_id
            for ti in tx['inputs']:
                tx_key = self._session.query(DbKey).filter_by(wallet_id=self.wallet_id, address=ti['address']).scalar()
                key_id = None
                if tx_key:
                    key_id = tx_key.id
                    key_ids.add(key_id)
                    tx_key.used = True
                db_tx_item = self._session.query(DbTransactionInput).\
                    filter_by(transaction_id=tx_id, input_n=ti['input_n']).scalar()
                if not db_tx_item:
                    new_tx_item = DbTransactionInput(transaction_id=tx_id, input_n=ti['input_n'], key_id=key_id,
                                                     value=ti['value'], prev_hash=ti['prev_hash'])
                    self._session.add(new_tx_item)
                    self._session.commit()
            for to in tx['outputs']:
                tx_key = self._session.query(DbKey).filter_by(wallet_id=self.wallet_id,
                                                              address=to['address']).scalar()
                key_id = None
                if tx_key:
                    key_id = tx_key.id
                    key_ids.add(key_id)
                    tx_key.used = True
                db_tx_item = self._session.query(DbTransactionOutput). \
                    filter_by(transaction_id=tx_id, output_n=to['output_n']).scalar()
                if not db_tx_item:
                    spent = to['spent']
                    if spent is None:
                        no_spent_info = True

                    new_tx_item = DbTransactionOutput(transaction_id=tx_id, output_n=to['output_n'], key_id=key_id,
                                                      value=to['value'], spent=spent)
                    self._session.add(new_tx_item)
                    self._session.commit()
        if no_spent_info:
            self._utxos_update_from_transactions(list(key_ids))
        return True

    def transactions(self, account_id=None, network=None, key_id=None):
        """

        :return list: List of transactions
        """

        network, account_id, acckey = self._get_account_defaults(network, account_id)

        qr = self._session.query(DbTransactionInput, DbKey.address, DbTransaction.confirmations,
                                 DbTransaction.hash, DbKey.network_name). \
            join(DbTransaction).join(DbKey). \
            filter(DbKey.account_id == account_id,
                   DbTransaction.wallet_id == self.wallet_id,
                   DbKey.network_name == network)
        if key_id is not None:
            qr = qr.filter(DbKey.id == key_id)
        txs = qr.all()

        qr = self._session.query(DbTransactionOutput, DbKey.address, DbTransaction.confirmations,
                                 DbTransaction.hash, DbKey.network_name). \
            join(DbTransaction).join(DbKey). \
            filter(DbKey.account_id == account_id,
                   DbTransaction.wallet_id == self.wallet_id,
                   DbKey.network_name == network)
        if key_id is not None:
            qr = qr.filter(DbKey.id == key_id)
        txs += qr.all()

        txs = sorted(txs, key=lambda k: (k[2], k[3]), reverse=True)

        res = []
        for tx in txs:
            u = tx[0].__dict__
            if '_sa_instance_state' in u:
                del u['_sa_instance_state']
            u['address'] = tx[1]
            u['confirmations'] = int(tx[2])
            u['tx_hash'] = tx[3]
            u['network_name'] = tx[4]
            if 'input_n' in u:
                u['value'] = -u['value']
            res.append(u)
        return res

    @staticmethod
    def _select_inputs(amount, utxo_query=None, max_utxos=None):
        """
        Internal method used by create transaction to select best inputs (UTXO's) for a transaction. To get the
        least number of inputs
        
        Example of UTXO query:
            SELECT transactions.id AS transactions_id, transactions.key_id AS transactions_key_id, 
            transactions.tx_hash AS transactions_tx_hash, transactions.date AS transactions_date, 
            transactions.confirmations AS transactions_confirmations, transactions.output_n AS transactions_output_n, 
            transactions."index" AS transactions_index, transactions.value AS transactions_value, 
            transactions.script AS transactions_script, transactions.description AS transactions_description, 
            transactions.spent AS transactions_spent
            FROM transactions JOIN keys ON keys.id = transactions.key_id 
            WHERE (transactions.spent IS ?) AND transactions.confirmations >= ? AND
            keys.account_id = ? AND keys.wallet_id = ?
        
        :param amount: Amount to transfer
        :type amount: int
        :param utxo_query: List of outputs in SQLalchemy query format. Wallet and Account ID filter must be included already. 
        :type utxo_query: self._session.query
        :param max_utxos: Maximum number of UTXO's to use. Set to 1 for optimal privacy. Default is None: No maximum
        :type max_utxos: int
        
        :return list: List of selected UTXO 
        """

        if not utxo_query:
            return []

        # Try to find one utxo with exact amount or higher
        one_utxo = utxo_query.\
            filter(DbTransactionOutput.spent.op("IS")(False), DbTransactionOutput.value >= amount).\
            order_by(DbTransactionOutput.value).first()
        if one_utxo:
            return [one_utxo]
        elif max_utxos and max_utxos <= 1:
            _logger.info("No single UTXO found with requested amount, use higher 'max_utxo' setting to use "
                         "multiple UTXO's")
            return []

        # Otherwise compose of 2 or more lesser outputs
        lessers = utxo_query.\
            filter(DbTransactionOutput.spent.op("IS")(False), DbTransactionOutput.value < amount).\
            order_by(DbTransactionOutput.value.desc()).all()
        total_amount = 0
        selected_utxos = []
        for utxo in lessers[:max_utxos]:
            if total_amount < amount:
                selected_utxos.append(utxo)
                total_amount += utxo.value
        if total_amount < amount:
            return []
        return selected_utxos

    def transaction_create(self, output_arr, input_arr=None, account_id=None, network=None, transaction_fee=None,
                           min_confirms=1, max_utxos=None):
        """
            Create new transaction with specified outputs. 
            Inputs can be specified but if not provided they will be selected from wallets utxo's.
            Output array is a list of 1 or more addresses and amounts.

            :param output_arr: List of output tuples with address and amount. Must contain at least one item. Example: [('mxdLD8SAGS9fe2EeCXALDHcdTTbppMHp8N', 5000000)] 
            :type output_arr: list 
            :param input_arr: List of inputs tuples with reference to a UTXO, a wallet key and value. The format is [(tx_hash, output_n, key_ids, value, signatures, unlocking_script)]
            :type input_arr: list
            :param account_id: Account ID
            :type account_id: int
            :param network: Network name. Leave empty for default network
            :type network: str
            :param transaction_fee: Set fee manually, leave empty to calculate fees automatically. Set fees in smallest currency denominator, for example satoshi's if you are using bitcoins
            :type transaction_fee: int
            :param min_confirms: Minimal confirmation needed for an UTXO before it will included in inputs. Default is 1 confirmation. Option is ignored if input_arr is provided.
            :type min_confirms: int
            :param max_utxos: Maximum number of UTXO's to use. Set to 1 for optimal privacy. Default is None: No maximum
            :type max_utxos: int

            :return Transaction: object
        """

        # TODO: Add transaction_id as possible input in input_arr
        amount_total_output = 0
        network, account_id, acckey = self._get_account_defaults(network, account_id)

        if input_arr and max_utxos and len(input_arr) > max_utxos:
            raise WalletError("Input array contains %d UTXO's but max_utxos=%d parameter specified" %
                              (len(input_arr), max_utxos))
        # Create transaction and add outputs
        transaction = Transaction(network=network)
        if not isinstance(output_arr, list):
            raise WalletError("Output array must be a list of tuples with address and amount. "
                              "Use 'send_to' method to send to one address")
        for o in output_arr:
            if isinstance(o, Output):
                transaction.outputs.append(o)
                amount_total_output += o.amount
            else:
                amount_total_output += o[1]
                transaction.add_output(o[1], o[0])

        # Calculate fees
        srv = Service(network=network)
        transaction.fee = transaction_fee
        transaction.fee_per_kb = None
        fee_per_output = None
        tr_size = 100 + (1 * 150) + (len(output_arr) + 1 * 50)
        if transaction_fee is None:
            if not input_arr:
                transaction.fee_per_kb = srv.estimatefee()
                if transaction.fee_per_kb is False:
                    raise WalletError("Could not estimate transaction fees, please specify fees manually")
                transaction.fee = int((tr_size / 1024.0) * transaction.fee_per_kb)
                fee_per_output = int((50 / 1024) * transaction.fee_per_kb)
            else:
                transaction.fee = 0

        # Add inputs
        amount_total_input = 0
        if input_arr is None:
            utxo_query = self._session.query(DbTransactionOutput).join(DbTransaction).join(DbKey).\
                filter(DbTransaction.wallet_id == self.wallet_id,
                       DbKey.account_id == account_id,
                       DbTransactionOutput.spent.op("IS")(False),
                       DbTransaction.confirmations >= min_confirms)
            utxos = utxo_query.all()
            if not utxos:
                raise WalletError("Create transaction: No unspent transaction outputs found")
            input_arr = []
            selected_utxos = self._select_inputs(amount_total_output + transaction.fee, utxo_query, max_utxos)
            if not selected_utxos:
                raise WalletError("Not enough unspent transaction outputs found")
            for utxo in selected_utxos:
                amount_total_input += utxo.value
                input_arr.append((utxo.transaction.hash, utxo.output_n, utxo.key_id, utxo.value, []))
        else:
            for i, inp in enumerate(input_arr):
                # FIXME: Dirty stuff, please rewrite...
                if isinstance(inp, Input):
                    inp = (inp.prev_hash, inp.output_index, None, 0, inp.signatures, inp.unlocking_script)
                # Get key_ids, value from Db if not specified
                if not (inp[2] or inp[3]):
                    inp_utxo = self._session.query(DbTransactionOutput).join(DbTransaction).join(DbKey). \
                        filter(DbTransaction.wallet_id == self.wallet_id,
                               DbTransaction.hash == to_hexstring(inp[0]),
                               DbTransactionOutput.output_n == struct.unpack('>I', inp[1])[0]).first()
                    if not inp_utxo:
                        raise WalletError("UTXO %s not found in this wallet. Please update UTXO's" %
                                          to_hexstring(inp[0]))
                    input_arr[i] = (inp[0], inp[1], inp_utxo.key_id, inp_utxo.value)
                    amount_total_input = inp_utxo.value
                else:
                    amount_total_input += inp[3]
                if len(inp) > 4:
                    input_arr[i] += (inp[4],)
                if len(inp) > 5:
                    input_arr[i] += (inp[5],)

        if transaction_fee is False:
            transaction.change = 0
        else:
            transaction.change = int(amount_total_input - (amount_total_output + transaction.fee))

        if transaction.change < 0:
            raise WalletError("Total amount of outputs is greater then total amount of inputs")
        # If change amount is smaller then estimated fee it will cost to send it then skip change
        if fee_per_output and transaction.change < fee_per_output:
            transaction.change = 0
        ck = None
        if transaction.change:
            # key_depth = 5
            ck = self.get_key(account_id=account_id, network=network, change=1)
            transaction.add_output(transaction.change, ck.address)
            amount_total_output += transaction.change

        # TODO: Extra check for ridiculous fees
        # if (amount_total_input - amount_total_output) > tr_size * MAXIMUM_FEE_PER_KB

        # Add inputs
        for inp in input_arr:
            key = self._session.query(DbKey).filter_by(id=inp[2]).scalar()
            if not key:
                raise WalletError("Key '%s' not found in this wallet" % inp[2])
            if key.key_type == 'multisig':
                inp_keys = []
                for ck in key.multisig_children:
                    inp_keys.append(HDKey(ck.child_key.wif).key)
                script_type = 'p2sh_multisig'
            elif key.key_type in ['bip32', 'single']:
                inp_keys = HDKey(key.wif, compressed=key.compressed).key
                script_type = 'p2pkh'
            else:
                raise WalletError("Input key type %s not supported" % key.key_type)
            inp_id = transaction.add_input(inp[0], inp[1], keys=inp_keys, script_type=script_type,
                                           sigs_required=self.multisig_n_required, sort=self.sort_keys,
                                           compressed=key.compressed)
            # FIXME: This dirty stuff needs to be rewritten...
            if len(inp) > 4:
                transaction.inputs[inp_id].signatures += inp[4]
            if len(inp) > 5:
                transaction.inputs[inp_id].unlocking_script = inp[5]
            if transaction.inputs[inp_id].address != key.address:
                raise WalletError("Created input address is different from address of used key. Possibly wrong key "
                                  "order in multisig?")

        return transaction

    def transaction_import(self, raw_tx):
        """
        Import a raw transaction. Link inputs to wallet keys if possible and return Transaction object

        :param raw_tx: Raw transaction
        :type raw_tx: str, bytes

        :return Transaction:

        """
        t_import = Transaction.import_raw(raw_tx, network=self.network.network_name)
        return self.transaction_create(t_import.outputs, t_import.inputs, transaction_fee=False)

    def transaction_sign(self, transaction, private_keys=None):
        """
        Sign transaction with private keys available in this wallet or extra private_keys specified.
        Return a signed transaction

        :param transaction: A transaction object
        :type transaction: Transaction
        :param private_keys: Extra private keys
        :type private_keys: list, HDKey, bytes, str

        :return Transaction: A transaction with one or more signed keys
        """
        priv_key_list_arg = []
        if private_keys:
            if not isinstance(private_keys, list):
                private_keys = [private_keys]
            for priv_key in private_keys:
                if isinstance(priv_key, HDKey):
                    priv_key_list_arg.append(priv_key)
                else:
                    priv_key_list_arg.append(HDKey(priv_key))
        for ti in transaction.inputs:
            priv_key_list = deepcopy(priv_key_list_arg)
            for k in ti.keys:
                if k.isprivate:
                    if isinstance(k, HDKey):
                        hdkey = k
                    else:
                        hdkey = HDKey(k)
                    if hdkey not in priv_key_list:
                        priv_key_list.append(k)
                elif self.cosigner:
                    # Check if private key is available in wallet
                    cosign_wallet_ids = [w.wallet_id for w in self.cosigner]
                    db_pk = self._session.query(DbKey).filter_by(public=k.public_hex, is_private=True).\
                        filter(DbKey.wallet_id.in_(cosign_wallet_ids + [self.wallet_id])).first()
                    if db_pk:
                        priv_key_list.append(HDKey(db_pk.wif))
            transaction.sign(priv_key_list, ti.tid)
        return transaction

    def transaction_send(self, transaction, offline=False):
        """
        Verify and push transaction to network. Update UTXO's in database after successfull send

        :param transaction: A signed transaction
        :type transaction: Transaction
        :param offline: Just return the transaction object and do not send it when offline = True. Default is False
        :type offline: bool

        :return str, dict: Transaction ID if successfull or dict with results otherwise

        """
        # Verify transaction
        if not transaction.verify():
            return {
                'error': "Cannot verify transaction. Create transaction failed",
                'transaction': transaction
            }

        if offline:
            return {
                'transaction': transaction
            }

        # Push it to the network
        srv = Service(network=transaction.network.network_name)
        res = srv.sendrawtransaction(transaction.raw_hex())
        if not res:
            # raise WalletError("Could not send transaction: %s" % srv.errors)
            return {
                'error': "Cannot send transaction. %s" % srv.errors,
                'transaction': transaction
            }
        _logger.info("Successfully pushed transaction, result: %s" % res)

        # Update db: Update spent UTXO's, add transaction to database
        for inp in transaction.inputs:
            tx_hash = to_hexstring(inp.prev_hash)
            utxos = self._session.query(DbTransactionOutput).join(DbTransaction).\
                filter(DbTransaction.hash == tx_hash,
                       DbTransactionOutput.output_n == inp.output_index_int).all()
            for u in utxos:
                u.spent = True

        self._session.commit()
        if 'txid' in res:
            return res['txid']
        else:
            return res

    def send(self, output_arr, input_arr=None, account_id=None, network=None, transaction_fee=None, min_confirms=4,
             priv_keys=None, max_utxos=None, offline=False):
        """
        Create new transaction with specified outputs and push it to the network. 
        Inputs can be specified but if not provided they will be selected from wallets utxo's.
        Output array is a list of 1 or more addresses and amounts.
        
        :param output_arr: List of output tuples with address and amount. Must contain at least one item. Example: [('mxdLD8SAGS9fe2EeCXALDHcdTTbppMHp8N', 5000000)] 
        :type output_arr: list 
        :param input_arr: List of inputs tuples with reference to a UTXO, a wallet key and value. The format is [(tx_hash, output_n, key_id, value)]
        :type input_arr: list
        :param account_id: Account ID
        :type account_id: int
        :param network: Network name. Leave empty for default network
        :type network: str
        :param transaction_fee: Set fee manually, leave empty to calculate fees automatically. Set fees in smallest currency denominator, for example satoshi's if you are using bitcoins
        :type transaction_fee: int
        :param min_confirms: Minimal confirmation needed for an UTXO before it will included in inputs. Default is 4. Option is ignored if input_arr is provided.
        :type min_confirms: int
        :param priv_keys: Specify extra private key if not available in this wallet
        :type priv_keys: HDKey, list
        :param max_utxos: Maximum number of UTXO's to use. Set to 1 for optimal privacy. Default is None: No maximum
        :type max_utxos: int

        :return str, list: Transaction ID or result array
        """

        if input_arr and max_utxos and len(input_arr) > max_utxos:
            raise WalletError("Input array contains %d UTXO's but max_utxos=%d parameter specified" %
                              (len(input_arr), max_utxos))

        transaction = self.transaction_create(output_arr, input_arr, account_id, network, transaction_fee,
                                              min_confirms, max_utxos)
        transaction = self.transaction_sign(transaction, priv_keys)
        # Calculate exact estimated fees and update change output if necessary
        if transaction_fee is None and transaction.fee_per_kb and transaction.change:
            fee_exact = transaction.estimate_fee()
            # Recreate transaction if fee estimation more then 10% off
            if fee_exact and abs((transaction.fee - fee_exact) / float(fee_exact)) > 0.10:
                _logger.info("Transaction fee not correctly estimated (est.: %d, real: %d). "
                             "Recreate transaction with correct fee" % (transaction.fee, fee_exact))
                transaction = self.transaction_create(output_arr, input_arr, account_id, network, fee_exact,
                                                      min_confirms, max_utxos)
                transaction = self.transaction_sign(transaction, priv_keys)

        return self.transaction_send(transaction, offline)

    def send_to(self, to_address, amount, account_id=None, network=None, transaction_fee=None, min_confirms=4,
                priv_keys=None, offline=False):
        """
        Create transaction and send it with default Service objects sendrawtransaction method

        :param to_address: Single output address
        :type to_address: str
        :param amount: Output is smallest denominator for this network (ie: Satoshi's for Bitcoin)
        :type amount: int
        :param account_id: Account ID, default is last used
        :type account_id: int
        :param network: Network name. Leave empty for default network
        :type network: str
        :param transaction_fee: Fee to use for this transaction. Leave empty to automatically estimate.
        :type transaction_fee: int
        :param min_confirms: Minimal confirmation needed for an UTXO before it will included in inputs. Default is 4. Option is ignored if input_arr is provided.
        :type min_confirms: int
        :param priv_keys: Specify extra private key if not available in this wallet
        :type priv_keys: HDKey, list

        :return str, list: Transaction ID or result array 
        """

        outputs = [(to_address, amount)]
        return self.send(outputs, account_id=account_id, network=network, transaction_fee=transaction_fee,
                         min_confirms=min_confirms, priv_keys=priv_keys, offline=offline)

    def sweep(self, to_address, account_id=None, input_key_id=None, network=None, max_utxos=999, min_confirms=1,
              fee_per_kb=None, offline=False):
        """
        Sweep all unspent transaction outputs (UTXO's) and send them to one output address. 
        Wrapper for the send method.
        
        :param to_address: Single output address
        :type to_address: str
        :param account_id: Wallet's account ID
        :type account_id: int
        :param input_key_id: Limit sweep to UTXO's with this key_id
        :type input_key_id: int
        :param network: Network name. Leave empty for default network
        :type network: str
        :param max_utxos: Limit maximum number of outputs to use. Default is 999
        :type max_utxos: int
        :param min_confirms: Minimal confirmations needed to include utxo
        :type min_confirms: int
        :param fee_per_kb: Fee per kilobyte transaction size, leave empty to get estimated fee costs from Service provider.
        :type fee_per_kb: int
        
        :return str, list: Transaction ID or result array
        """

        network, account_id, acckey = self._get_account_defaults(network, account_id)

        utxos = self.utxos(account_id=account_id, network=network, min_confirms=min_confirms, key_id=input_key_id)
        utxos = utxos[0:max_utxos]
        input_arr = []
        total_amount = 0
        if not utxos:
            return False
        for utxo in utxos:
            # Skip dust transactions
            if utxo['value'] < self.network.dust_ignore_amount:
                continue
            input_arr.append((utxo['tx_hash'], utxo['output_n'], utxo['key_id'], utxo['value']))
            total_amount += utxo['value']
        srv = Service(network=network)
        if fee_per_kb is None:
            fee_per_kb = srv.estimatefee()
        tr_size = 125 + (len(input_arr) * 125)
        estimated_fee = int((tr_size / 1024.0) * fee_per_kb)
        return self.send([(to_address, total_amount-estimated_fee)], input_arr, network=network,
                         transaction_fee=estimated_fee, min_confirms=min_confirms, offline=offline)

    def info(self, detail=3):
        """
        Prints wallet information to standard output
        
        :param detail: Level of detail to show. Specify a number between 0 and 4, with 0 low detail and 4 highest detail
        :type detail: int

        """
        print("=== WALLET ===")
        print(" ID                             %s" % self.wallet_id)
        print(" Name                           %s" % self.name)
        print(" Owner                          %s" % self._owner)
        print(" Scheme                         %s" % self.scheme)
        if self.scheme == 'multisig':
            print(" Multisig Wallet IDs            %s" % str([w.wallet_id for w in self.cosigner]).strip('[]'))
        print(" Main network                   %s" % self.network.network_name)
        print(" Balance                        %s\n" % self.balance(as_string=True))

        if self.scheme == 'multisig':
            print("= Multisig main keys =")
            for mk_wif in [w.main_key.wif for w in self.cosigner]:
                print(mk_wif)

        if detail and self.main_key:
            print("\n= Main key =")
            self.main_key.dict()
        if detail > 1:
            for nw in self.networks():
                print("\n- Network: %s -" % nw['network_name'])
                if detail < 3:
                    ds = [0, 3, 5]
                else:
                    ds = range(6)
                for d in ds:
                    is_active = True
                    if detail > 3:
                        is_active = False
                    for key in self.keys(depth=d, network=nw['network_name'], is_active=is_active):
                        print("%5s %-28s %-45s %-25s %25s" % (key.id, key.path, key.address, key.name,
                                                              Network(key.network_name).print_value(key.balance)))
        print("\n= Transactions =")
        if detail > 2:
            for t in self.transactions():
                spent = ""
                if 'spent' in t and t['spent'] is False:
                    spent = "U"
                print("%64s %36s %8d %13d %s" % (t['tx_hash'], t['address'], t['confirmations'], t['value'], spent))

        print("\n")

    def dict(self, detail=3):
        """
        Return wallet information in dictionary format

        :param detail: Level of detail to show, can be 0, 1, 2 or 3
        :type detail: int

        :return dict:
        """

        # if detail and self.main_key:
        #     self.main_key.info()
        if detail > 1:
            for nw in self.networks():
                print("- Network: %s -" % nw['network_name'])
                if detail < 3:
                    ds = [0, 3, 5]
                else:
                    ds = range(6)
                for d in ds:
                    for key in self.keys(depth=d, network=nw['network_name']):
                        print("%5s %-28s %-45s %-25s %25s" % (key.id, key.path, key.address, key.name,
                                                              Network(key.network_name).print_value(key.balance)))

        return {
            'wallet_id': self.wallet_id,
            'name': self.name,
            'owner': self._owner,
            'scheme': self.scheme,
            'main_network': self.network.network_name,
            'main_balance': self.balance(),
            'main_balance_str': self.balance(as_string=True),
            'balances': self._balances,
            'default_account_id': self.default_account_id,
            'multisig_n_required': self.multisig_n_required,
            'multisig_compressed': self.multisig_compressed,
            'cosigner_wallet_ids': [w.wallet_id for w in self.cosigner],
            'cosigner_mainkey_wifs': [w.main_key.wif for w in self.cosigner],
            'sort_keys': self.sort_keys,
            # 'main_key': self.main_key.dict(),
            'main_key_id': self.main_key_id
        }
